"""Расчёт размера позиции, стоп-лосса и тейк-профита.

Логика RR (risk/reward):
1. TP определяется схождением спреда -> известна дистанция тейка по цене.
2. Дистанция стопа = дистанция тейка / RR (RR=1 -> 1:1, RR=2 -> стоп вдвое ближе тейка).
3. Размер позиции (нотионал/маржа) подбирается так, чтобы при срабатывании
   стопа терялось ровно settings['stop_loss_pct'] % депозита:
   notional = max_loss / stop_distance.
"""
import logging
import math
from dataclasses import dataclass

log = logging.getLogger("risk")


@dataclass
class PositionPlan:
    symbol: str
    direction: str            # "LONG" | "SHORT"
    leverage: int
    vol: float                # объём в контрактах
    notional_usd: float
    margin_usd: float
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    stop_distance_pct: float
    tp_distance_pct: float
    max_loss_usd: float
    expected_profit_usd: float


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def build_position_plan(
    *,
    symbol: str,
    direction: str,
    balance_usd: float,
    spread_pct: float,          # абсолютное значение спреда на входе, %
    mexc_price: float,          # текущая цена MEXC (вход по рынку)
    binance_price: float,       # референсная цена
    contract: dict,             # объект из contract/detail
    settings: dict,
) -> tuple[PositionPlan | None, str]:
    """Возвращает (план, причина-отказа). План = None, если входить не стоит."""

    max_leverage = min(int(contract.get("maxLeverage", 20)), int(settings["max_leverage_cap"]))
    contract_size = float(contract.get("contractSize", 1))
    vol_step = float(contract.get("volUnit", 1)) or 1
    min_vol = float(contract.get("minVol", 1)) or 1
    price_scale = int(contract.get("priceScale", 4))

    rr = float(settings.get("risk_reward", 1.0))  # RR = дистанция TP / дистанция SL
    max_loss_usd = balance_usd * settings["stop_loss_pct"] / 100.0

    # ------------------------------------------------------------------
    # 1. TP: цена MEXC, при которой спред сойдётся к exit_threshold
    # ------------------------------------------------------------------
    if direction == "LONG":
        tp = round(binance_price * (1 - settings["exit_threshold"] / 100.0), price_scale)
        if tp <= mexc_price:
            return None, "TP ниже цены входа (спред уже сошёлся)"
        tp_distance_pct = (tp - mexc_price) / mexc_price * 100.0
    else:
        tp = round(binance_price * (1 + settings["exit_threshold"] / 100.0), price_scale)
        if tp >= mexc_price:
            return None, "TP выше цены входа (спред уже сошёлся)"
        tp_distance_pct = (mexc_price - tp) / mexc_price * 100.0

    # ------------------------------------------------------------------
    # 2. SL: дистанция стопа по цене = дистанция тейка / RR
    # ------------------------------------------------------------------
    stop_distance_pct = tp_distance_pct / rr
    min_stop = settings["min_stop_distance_pct"]
    if stop_distance_pct < min_stop:
        return None, (
            f"стоп {stop_distance_pct:.3f}% ближе минимума {min_stop}% "
            f"(TP-дистанция {tp_distance_pct:.3f}%, RR 1:{rr:g}) — спред слишком мал"
        )

    # ------------------------------------------------------------------
    # 3. Размер позиции: при пробое стопа теряем ровно max_loss_usd
    # ------------------------------------------------------------------
    notional = max_loss_usd / (stop_distance_pct / 100.0)

    # маржа при максимальном плече; предохранитель — не больше position_pct% депо
    margin_usd = notional / max_leverage
    margin_cap = balance_usd * settings["position_pct"] / 100.0
    if margin_usd > margin_cap:
        # уменьшаем позицию: убыток при стопе станет МЕНЬШЕ max_loss (безопасно)
        notional = margin_cap * max_leverage
        margin_usd = margin_cap

    # стоп обязан сработать раньше ликвидации (изолированная маржа):
    # ликвидация ~ на дистанции (1/leverage)*100% минус maintenance (запас 20%)
    liq_distance_pct = (1.0 / max_leverage) * 100.0 * 0.8
    if stop_distance_pct >= liq_distance_pct:
        # снижаем плечо (нотионал не меняется — растёт только маржа)
        needed_lev = max(1, int((100.0 * 0.8) / (stop_distance_pct * 1.25)))
        max_leverage = min(max_leverage, needed_lev)
        margin_usd = notional / max_leverage
        if margin_usd > margin_cap:
            notional = margin_cap * max_leverage
            margin_usd = margin_cap

    # ------------------------------------------------------------------
    # 4. Фильтр «почти убыточных» входов: ожидаемый профит при тейке
    # ------------------------------------------------------------------
    expected_move_pct = tp_distance_pct - settings["slippage_pct"]
    expected_profit_usd = expected_move_pct / 100.0 * notional
    min_profit_usd = balance_usd * settings["min_profit_pct"] / 100.0
    if expected_profit_usd < min_profit_usd:
        return None, (
            f"ожидаемый профит {expected_profit_usd:.2f}$ < минимума {min_profit_usd:.2f}$ "
            f"(спред {spread_pct:.3f}%)"
        )

    # ------------------------------------------------------------------
    # 5. Объём в контрактах + пересчёт под округление
    # ------------------------------------------------------------------
    raw_vol = notional / (mexc_price * contract_size)
    vol = _round_step(raw_vol, vol_step)
    if vol < min_vol:
        return None, f"объём {raw_vol:.4f} меньше минимального {min_vol} контрактов"
    actual_notional = vol * mexc_price * contract_size
    actual_max_loss = stop_distance_pct / 100.0 * actual_notional
    actual_profit = expected_move_pct / 100.0 * actual_notional

    if direction == "LONG":
        sl = round(mexc_price * (1 - stop_distance_pct / 100.0), price_scale)
    else:
        sl = round(mexc_price * (1 + stop_distance_pct / 100.0), price_scale)

    return (
        PositionPlan(
            symbol=symbol,
            direction=direction,
            leverage=max_leverage,
            vol=vol,
            notional_usd=actual_notional,
            margin_usd=actual_notional / max_leverage,
            entry_price=mexc_price,
            stop_loss_price=sl,
            take_profit_price=tp,
            stop_distance_pct=stop_distance_pct,
            tp_distance_pct=tp_distance_pct,
            max_loss_usd=actual_max_loss,
            expected_profit_usd=actual_profit,
        ),
        "",
    )
