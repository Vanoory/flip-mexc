"""Расчёт размера позиции, стоп-лосса и тейк-профита.

Ключевая идея: стоп-лосс считается ОТ ДЕПОЗИТА, а не от цены.
При срабатывании стопа теряется ровно settings['stop_loss_pct'] % депозита.
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

    max_loss_usd = balance_usd * settings["stop_loss_pct"] / 100.0
    margin_usd = balance_usd * settings["position_pct"] / 100.0
    notional = margin_usd * max_leverage

    # Дистанция стопа в % от цены, при которой убыток = max_loss_usd
    stop_distance_pct = max_loss_usd / notional * 100.0

    # Если стоп получился слишком близко (шум стакана выбьет мгновенно) —
    # уменьшаем нотионал так, чтобы стоп был не ближе минимума,
    # а убыток при стопе остался равным max_loss_usd.
    min_stop = settings["min_stop_distance_pct"]
    if stop_distance_pct < min_stop:
        notional = max_loss_usd / (min_stop / 100.0)
        margin_usd = notional / max_leverage
        stop_distance_pct = min_stop

    # Стоп обязан сработать раньше ликвидации: маржа изолированная,
    # ликвидация ~ на дистанции (1/leverage)*100% минус maintenance.
    liq_distance_pct = (1.0 / max_leverage) * 100.0 * 0.8  # 20% запас на maintenance margin
    if stop_distance_pct >= liq_distance_pct:
        # снижаем плечо, чтобы стоп был до ликвидации
        needed_lev = max(1, int((100.0 * 0.8) / (stop_distance_pct * 1.5)))
        max_leverage = min(max_leverage, needed_lev)
        notional = margin_usd * max_leverage
        stop_distance_pct = max_loss_usd / notional * 100.0
        if stop_distance_pct < min_stop:
            notional = max_loss_usd / (min_stop / 100.0)
            margin_usd = notional / max_leverage
            stop_distance_pct = min_stop

    # Фильтр «почти убыточных» входов: ожидаемый ход = спред минус порог выхода
    # минус проскальзывание; профит должен быть >= min_profit_pct от депозита.
    expected_move_pct = abs(spread_pct) - settings["exit_threshold"] - settings["slippage_pct"]
    expected_profit_usd = expected_move_pct / 100.0 * notional
    min_profit_usd = balance_usd * settings["min_profit_pct"] / 100.0
    if expected_profit_usd < min_profit_usd:
        return None, (
            f"ожидаемый профит {expected_profit_usd:.2f}$ < минимума {min_profit_usd:.2f}$ "
            f"(спред {spread_pct:.3f}%)"
        )

    # Объём в контрактах
    raw_vol = notional / (mexc_price * contract_size)
    vol = _round_step(raw_vol, vol_step)
    if vol < min_vol:
        return None, f"объём {raw_vol:.4f} меньше минимального {min_vol} контрактов"
    actual_notional = vol * mexc_price * contract_size
    # пересчёт стопа под фактический нотионал (округление объёма меняет цифры)
    stop_distance_pct = max(max_loss_usd / actual_notional * 100.0, min_stop)

    if direction == "LONG":
        sl = round(mexc_price * (1 - stop_distance_pct / 100.0), price_scale)
        # TP: цена MEXC, при которой спред сойдётся к exit_threshold
        tp = round(binance_price * (1 - settings["exit_threshold"] / 100.0), price_scale)
        if tp <= mexc_price:
            return None, "TP ниже цены входа (спред уже сошёлся)"
    else:
        sl = round(mexc_price * (1 + stop_distance_pct / 100.0), price_scale)
        tp = round(binance_price * (1 + settings["exit_threshold"] / 100.0), price_scale)
        if tp >= mexc_price:
            return None, "TP выше цены входа (спред уже сошёлся)"

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
            max_loss_usd=max_loss_usd,
            expected_profit_usd=expected_profit_usd,
        ),
        "",
    )
