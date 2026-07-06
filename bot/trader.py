"""Торговый цикл: открытие, сопровождение и закрытие позиций.

Одна позиция за раз; после закрытия — случайная пауза 30–120 сек (настраивается).
SL/TP ставятся на бирже при открытии + дублируются программным контролем.
"""
import asyncio
import logging
import random
import time
from typing import Awaitable, Callable

from binance_feed import BINANCE
from mexc_client import MEXC, MexcError
from risk import PositionPlan, build_position_plan
from spread_engine import ENGINE, Signal, mexc_to_binance_symbol
from state import STATE

log = logging.getLogger("trader")

SIDE_OPEN_LONG = 1
SIDE_CLOSE_SHORT = 2
SIDE_OPEN_SHORT = 3
SIDE_CLOSE_LONG = 4

Notifier = Callable[[str], Awaitable[None]]


class Trader:
    def __init__(self):
        self.notify: Notifier = self._noop
        self.cooldown_until: float = 0
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    @staticmethod
    async def _noop(_: str):
        pass

    # ------------------------------------------------------------------

    def start(self, notify: Notifier):
        self.notify = notify
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self):
        await self._reconcile_on_start()
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.last_error = str(e)
                log.exception("Ошибка торгового цикла")
                await self.notify(f"⚠️ Ошибка торгового цикла: {e}")
                await asyncio.sleep(10)
            await asyncio.sleep(2)

    async def _reconcile_on_start(self):
        """При старте сверяем локальное состояние с биржей (осиротевшие позиции)."""
        try:
            positions = await MEXC.open_positions()
        except Exception as e:
            log.warning("Не удалось получить позиции при старте: %s", e)
            return
        if positions and not STATE.open_position:
            syms = ", ".join(p.get("symbol", "?") for p in positions)
            await self.notify(
                f"⚠️ На бирже найдены открытые позиции вне контроля бота: {syms}. "
                f"Бот их не трогает — закройте вручную или /close не поможет."
            )
        if STATE.open_position and not positions:
            log.info("Локальная позиция не найдена на бирже — очищаем состояние")
            STATE.open_position = None
            STATE.save()

    # ------------------------------------------------------------------

    async def _tick(self):
        if STATE.open_position:
            await self._manage_position()
            return
        if not STATE.settings["trading_enabled"]:
            return
        if time.time() < self.cooldown_until:
            return

        await ENGINE.refresh_pairs()
        if not ENGINE.pairs:
            return

        sig = await ENGINE.scan()
        if not sig:
            return
        sig = await ENGINE.verify_signal(sig)
        if not sig:
            return
        await self._open_position(sig)

    # ------------------------------------------------------------------

    async def _open_position(self, sig: Signal):
        balance = await MEXC.usdt_balance()
        equity = float(balance.get("equity", 0)) or float(balance.get("availableBalance", 0))
        if equity <= 0:
            await self.notify("⚠️ Нулевой баланс USDT на фьючерсном аккаунте — торговля невозможна.")
            STATE.set_setting("trading_enabled", False)
            return
        if STATE.stats.get("initial_balance") is None:
            STATE.stats["initial_balance"] = equity
            STATE.save()

        plan, reject = build_position_plan(
            symbol=sig.symbol,
            direction=sig.direction,
            balance_usd=equity,
            spread_pct=sig.spread_pct,
            mexc_price=sig.mexc_price,
            binance_price=sig.binance_price,
            contract=sig.contract,
            settings=STATE.settings,
        )
        if not plan:
            log.info("Вход в %s отклонён: %s", sig.symbol, reject)
            return

        position_type = 1 if plan.direction == "LONG" else 2
        try:
            await MEXC.set_leverage(plan.symbol, plan.leverage, position_type)
        except Exception as e:
            log.warning("Не удалось выставить плечо %sx на %s: %s", plan.leverage, plan.symbol, e)

        side = SIDE_OPEN_LONG if plan.direction == "LONG" else SIDE_OPEN_SHORT
        try:
            await MEXC.place_order(
                symbol=plan.symbol,
                side=side,
                vol=plan.vol,
                leverage=plan.leverage,
                stop_loss_price=plan.stop_loss_price,
                take_profit_price=plan.take_profit_price,
            )
        except MexcError as e:
            self.last_error = str(e)
            await self.notify(f"❌ Не удалось открыть {plan.direction} {plan.symbol}: {e}")
            self._set_cooldown()
            return

        STATE.open_position = {
            "symbol": plan.symbol,
            "direction": plan.direction,
            "leverage": plan.leverage,
            "vol": plan.vol,
            "notional_usd": plan.notional_usd,
            "margin_usd": plan.margin_usd,
            "entry_price": plan.entry_price,
            "stop_loss_price": plan.stop_loss_price,
            "take_profit_price": plan.take_profit_price,
            "balance_at_entry": equity,
            "entry_spread_pct": sig.spread_pct,
            "opened_at": time.time(),
        }
        STATE.save()
        await self.notify(
            f"📈 Открыта позиция\n"
            f"{plan.symbol} | {plan.direction} | {plan.leverage}x\n"
            f"Маржа: {plan.margin_usd:.2f}$ | Нотионал: {plan.notional_usd:.2f}$\n"
            f"Вход: {plan.entry_price:.6g} | SL: {plan.stop_loss_price:.6g} | "
            f"TP: {plan.take_profit_price:.6g}\n"
            f"Спред на входе: {sig.spread_pct:+.3f}% | "
            f"Риск при стопе: {plan.max_loss_usd:.2f}$ "
            f"({STATE.settings['stop_loss_pct']}% депо)"
        )

    # ------------------------------------------------------------------

    async def _manage_position(self):
        pos = STATE.open_position
        if not pos:
            return
        settings = STATE.settings

        # позиция могла закрыться на бирже (SL/TP ордера)
        try:
            live = await MEXC.open_positions()
        except Exception:
            live = None
        if live is not None and not any(p.get("symbol") == pos["symbol"] for p in live):
            await self._finalize_closed_position(reason="SL/TP на бирже")
            return

        # текущие цены
        try:
            depth = await MEXC.depth(pos["symbol"])
            bids, asks = depth.get("bids", []), depth.get("asks", [])
            if not bids or not asks:
                return
            mexc_mid = (float(bids[0][0]) + float(asks[0][0])) / 2
        except Exception:
            return
        binance_mid = BINANCE.mid_price(mexc_to_binance_symbol(pos["symbol"]))
        spread = ((mexc_mid - binance_mid) / binance_mid * 100.0) if binance_mid else None

        is_long = pos["direction"] == "LONG"
        reason = None

        # программный дубль SL (на случай если биржевой стоп не встал)
        if is_long and mexc_mid <= pos["stop_loss_price"]:
            reason = "стоп-лосс (программный)"
        elif not is_long and mexc_mid >= pos["stop_loss_price"]:
            reason = "стоп-лосс (программный)"
        # TP по схождению спреда
        elif spread is not None:
            if is_long and spread >= -settings["exit_threshold"]:
                reason = "тейк-профит (спред сошёлся)"
            elif not is_long and spread <= settings["exit_threshold"]:
                reason = "тейк-профит (спред сошёлся)"
        # таймаут
        if reason is None and time.time() - pos["opened_at"] > settings["position_timeout_min"] * 60:
            reason = f"таймаут {settings['position_timeout_min']} мин"

        if reason:
            await self.close_position(reason=reason, exit_price=mexc_mid)

    async def close_position(self, reason: str, exit_price: float | None = None):
        pos = STATE.open_position
        if not pos:
            return
        side = SIDE_CLOSE_LONG if pos["direction"] == "LONG" else SIDE_CLOSE_SHORT
        try:
            await MEXC.place_order(
                symbol=pos["symbol"], side=side, vol=pos["vol"], leverage=pos["leverage"]
            )
        except MexcError as e:
            # позиция могла уже закрыться биржевым SL/TP
            log.warning("Ошибка закрытия %s: %s", pos["symbol"], e)
            try:
                live = await MEXC.open_positions()
                if any(p.get("symbol") == pos["symbol"] for p in live):
                    await self.notify(f"❌ НЕ УДАЛОСЬ закрыть {pos['symbol']}: {e} — закройте вручную!")
                    return
            except Exception:
                await self.notify(f"⚠️ Ошибка закрытия {pos['symbol']}: {e} — проверьте биржу!")
                return
        await self._finalize_closed_position(reason=reason, exit_price=exit_price)

    async def _finalize_closed_position(self, reason: str, exit_price: float | None = None):
        pos = STATE.open_position
        if not pos:
            return
        STATE.open_position = None

        # фактический PnL берём с биржи по изменению баланса
        pnl_usd = None
        try:
            await asyncio.sleep(2)  # даём бирже провести расчёт
            balance = await MEXC.usdt_balance()
            equity = float(balance.get("equity", 0)) or float(balance.get("availableBalance", 0))
            pnl_usd = equity - pos["balance_at_entry"]
        except Exception:
            pass
        if pnl_usd is None and exit_price:
            move = (exit_price - pos["entry_price"]) / pos["entry_price"]
            if pos["direction"] == "SHORT":
                move = -move
            pnl_usd = move * pos["notional_usd"]
        pnl_usd = pnl_usd or 0.0
        pnl_pct_depo = pnl_usd / pos["balance_at_entry"] * 100.0

        STATE.record_trade(
            {
                "symbol": pos["symbol"],
                "direction": pos["direction"],
                "pnl_usd": round(pnl_usd, 4),
                "pnl_pct_depo": round(pnl_pct_depo, 4),
                "reason": reason,
                "closed_at": time.time(),
            }
        )
        emoji = "✅" if pnl_usd >= 0 else "🔻"
        await self.notify(
            f"{emoji} {pos['symbol']} | {pos['direction']} | "
            f"{pnl_pct_depo:+.2f}% к депо / {pnl_usd:+.2f}$\n"
            f"Причина: {reason}"
        )
        self._set_cooldown()

    def _set_cooldown(self):
        s = STATE.settings
        pause = random.uniform(s["cooldown_min_sec"], s["cooldown_max_sec"])
        self.cooldown_until = time.time() + pause
        log.info("Кулдаун %.0f сек до следующего поиска сигнала", pause)


TRADER = Trader()
