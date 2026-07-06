"""Сканер спреда: отбор zero-fee пар и поиск сигналов MEXC vs Binance."""
import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from binance_feed import BINANCE
from mexc_client import MEXC
from state import STATE

log = logging.getLogger("spread")

PAIRS_REFRESH_SEC = 3600  # обновление списка пар раз в час


@dataclass
class Signal:
    symbol: str            # MEXC-формат, напр. BTC_USDT
    direction: str         # LONG | SHORT
    spread_pct: float      # подписанный спред (mexc-binance)/binance*100
    mexc_price: float
    binance_price: float
    contract: dict


def mexc_to_binance_symbol(mexc_symbol: str) -> str:
    return mexc_symbol.replace("_", "")


class SpreadEngine:
    def __init__(self):
        self.pairs: list[dict] = []        # контракты, прошедшие фильтры
        self._pairs_loaded_at: float = 0
        self._confirmations: dict[str, dict] = {}  # symbol -> {direction, count}
        self._price_history: dict[str, deque] = {}  # symbol -> deque[(ts, price)]
        self.last_refresh_error: str | None = None

    def _too_volatile(self, sym: str, price: float, max_move_pct: float) -> bool:
        """Анти-памп: пропускаем монету, если цена сдвинулась > max_move_pct за минуту."""
        now = time.time()
        hist = self._price_history.setdefault(sym, deque(maxlen=120))
        hist.append((now, price))
        old = None
        for ts, p in hist:
            if now - ts <= 90:      # берём самую старую точку в окне ~60-90 сек
                old = p
                break
        if old is None or old <= 0:
            return False
        move = abs(price - old) / old * 100.0
        return move > max_move_pct

    # ---------- отбор пар ----------

    async def refresh_pairs(self, force: bool = False):
        if not force and time.time() - self._pairs_loaded_at < PAIRS_REFRESH_SEC:
            return
        settings = STATE.settings
        details, zero_fee = await asyncio.gather(
            MEXC.contract_details(), MEXC.zero_fee_symbols()
        )
        if not BINANCE.futures_symbols and not BINANCE.spot_symbols:
            await BINANCE.load_symbol_lists()

        tickers = {t["symbol"]: t for t in await MEXC.all_tickers()}
        pairs = []
        for c in details:
            sym = c["symbol"]
            if not sym.endswith("_USDT"):
                continue
            if c.get("state", 0) != 0:  # 0 = торгуется
                continue
            if sym not in zero_fee:
                continue
            if sym in STATE.blacklist:  # пары, запрещённые биржей для региона
                continue
            if not BINANCE.has_symbol(mexc_to_binance_symbol(sym)):
                continue
            t = tickers.get(sym)
            vol24 = float(t.get("amount24", 0)) if t else 0
            if vol24 < settings["min_volume_24h_usd"]:
                continue
            pairs.append(c)

        self.pairs = pairs
        self._pairs_loaded_at = time.time()
        self.last_refresh_error = None if pairs else "После фильтров не осталось ни одной пары"
        log.info("Отобрано %d zero-fee пар для торговли", len(pairs))

    # ---------- сканирование ----------

    async def scan(self) -> Signal | None:
        """Один проход по всем парам. Возвращает лучший подтверждённый сигнал."""
        settings = STATE.settings
        threshold = settings["entry_threshold"]
        need_confirm = int(settings["signal_confirmations"])

        try:
            tickers = {t["symbol"]: t for t in await MEXC.all_tickers()}
        except Exception as e:
            log.warning("Не получили тикеры MEXC: %s", e)
            return None

        best: Signal | None = None
        seen_symbols = set()

        for c in self.pairs:
            sym = c["symbol"]
            if sym in STATE.blacklist:
                continue
            t = tickers.get(sym)
            if not t:
                continue
            bid, ask = float(t.get("bid1", 0)), float(t.get("ask1", 0))
            if bid <= 0 or ask <= 0:
                continue
            mexc_mid = (bid + ask) / 2
            # анти-памп: цена скачет — сигнал скорее всего "догоняющий", стоп проскользит
            if self._too_volatile(sym, mexc_mid, settings.get("max_move_1m_pct", 1.0)):
                self._confirmations.pop(sym, None)
                continue
            binance_mid = BINANCE.mid_price(mexc_to_binance_symbol(sym))
            if not binance_mid:
                continue

            spread = (mexc_mid - binance_mid) / binance_mid * 100.0
            direction = None
            if spread <= -threshold:
                direction = "LONG"    # MEXC дешевле — покупаем на MEXC
            elif spread >= threshold:
                direction = "SHORT"   # MEXC дороже — продаём на MEXC

            if direction is None:
                self._confirmations.pop(sym, None)
                continue

            seen_symbols.add(sym)
            conf = self._confirmations.get(sym)
            if conf and conf["direction"] == direction:
                conf["count"] += 1
            else:
                conf = {"direction": direction, "count": 1}
                self._confirmations[sym] = conf

            if conf["count"] < need_confirm:
                continue  # сигнал ещё не подтверждён — отсекаем выбросы

            sig = Signal(
                symbol=sym,
                direction=direction,
                spread_pct=spread,
                mexc_price=mexc_mid,
                binance_price=binance_mid,
                contract=c,
            )
            if best is None or abs(sig.spread_pct) > abs(best.spread_pct):
                best = sig

        # чистим подтверждения пар, у которых спред пропал
        for sym in list(self._confirmations):
            if sym not in seen_symbols:
                self._confirmations.pop(sym, None)

        return best

    async def check_liquidity(self, sig: Signal, notional_usd: float) -> tuple[bool, str]:
        """Глубина стакана: в пределах depth_range_pct от цены должно лежать
        минимум depth_multiplier * размер позиции С ОБЕИХ сторон — иначе стоп
        проскользит сквозь тонкий стакан и убыток будет кратно больше плана."""
        settings = STATE.settings
        rng = settings.get("depth_range_pct", 0.3) / 100.0
        mult = settings.get("depth_multiplier", 20.0)
        contract_size = float(sig.contract.get("contractSize", 1))
        try:
            depth = await MEXC.depth(sig.symbol, limit=50)
        except Exception as e:
            return False, f"не получили стакан: {e}"
        bids, asks = depth.get("bids", []), depth.get("asks", [])
        if not bids or not asks:
            return False, "пустой стакан"
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2

        def side_depth_usd(levels) -> float:
            total = 0.0
            for lv in levels:
                price, vol = float(lv[0]), float(lv[1])
                if abs(price - mid) / mid > rng:
                    break
                total += price * vol * contract_size
            return total

        bid_usd, ask_usd = side_depth_usd(bids), side_depth_usd(asks)
        need = notional_usd * mult
        weakest = min(bid_usd, ask_usd)
        if weakest < need:
            return False, (
                f"тонкий стакан: {weakest:,.0f}$ в ±{rng*100:.1f}% от цены, "
                f"нужно ≥{need:,.0f}$ ({mult:g}x позиции {notional_usd:,.0f}$)"
            )
        return True, ""

    async def verify_signal(self, sig: Signal) -> Signal | None:
        """Перед входом перепроверяем спред по стакану MEXC (а не по тикеру)."""
        try:
            depth = await MEXC.depth(sig.symbol)
            bids, asks = depth.get("bids", []), depth.get("asks", [])
            if not bids or not asks:
                return None
            mexc_mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        except Exception as e:
            log.warning("Не получили стакан %s: %s", sig.symbol, e)
            return None
        binance_mid = BINANCE.mid_price(mexc_to_binance_symbol(sig.symbol), max_age_sec=5)
        if not binance_mid:
            return None
        spread = (mexc_mid - binance_mid) / binance_mid * 100.0
        threshold = STATE.settings["entry_threshold"]
        if sig.direction == "LONG" and spread > -threshold:
            return None
        if sig.direction == "SHORT" and spread < threshold:
            return None
        sig.spread_pct = spread
        sig.mexc_price = mexc_mid
        sig.binance_price = binance_mid
        return sig


ENGINE = SpreadEngine()
