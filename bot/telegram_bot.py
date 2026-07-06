"""Telegram-бот управления (aiogram v3). Доступ только для TELEGRAM_ADMIN_ID."""
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config import CREDS
from mexc_client import MEXC
from spread_engine import ENGINE
from state import STATE
from trader import TRADER

log = logging.getLogger("telegram")

bot = Bot(token=CREDS.telegram_bot_token) if CREDS.telegram_bot_token else None
dp = Dispatcher()


def _is_admin(user_id: int) -> bool:
    return user_id == CREDS.telegram_admin_id


async def notify_admin(text: str):
    if bot and CREDS.telegram_admin_id:
        try:
            await bot.send_message(CREDS.telegram_admin_id, text)
        except Exception as e:
            log.warning("Не удалось отправить сообщение в Telegram: %s", e)


def _status_keyboard() -> InlineKeyboardMarkup:
    enabled = STATE.settings["trading_enabled"]
    toggle = (
        InlineKeyboardButton(text="⏸ Остановить торговлю", callback_data="stop")
        if enabled
        else InlineKeyboardButton(text="▶️ Запустить торговлю", callback_data="start")
    )
    row2 = [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")]
    if STATE.open_position:
        row2.append(InlineKeyboardButton(text="✖️ Закрыть позицию", callback_data="close"))
    return InlineKeyboardMarkup(inline_keyboard=[[toggle], row2])


def _status_text() -> str:
    s = STATE.settings
    lines = [
        f"Торговля: {'🟢 ВКЛ' if s['trading_enabled'] else '🔴 ВЫКЛ'}",
        f"Стоп: {s['stop_loss_pct']}% депо | Вход: спред ≥ {s['entry_threshold']}%",
        f"Маржа на сделку: {s['position_pct']}% депо | "
        f"Кулдаун: {s['cooldown_min_sec']}–{s['cooldown_max_sec']} сек",
        f"Пар в списке: {len(ENGINE.pairs)}",
    ]
    pos = STATE.open_position
    if pos:
        age_min = (time.time() - pos["opened_at"]) / 60
        lines.append(
            f"\nПозиция: {pos['symbol']} {pos['direction']} {pos['leverage']}x, "
            f"маржа {pos['margin_usd']:.2f}$, открыта {age_min:.0f} мин назад"
        )
    else:
        wait = max(0, TRADER.cooldown_until - time.time())
        lines.append(f"\nПозиций нет" + (f" (кулдаун ещё {wait:.0f} сек)" if wait else ""))
    if TRADER.last_error:
        lines.append(f"\n⚠️ Последняя ошибка: {TRADER.last_error[:200]}")
    return "\n".join(lines)


def _stats_text() -> str:
    st = STATE.stats
    total = st["total_trades"]
    winrate = (st["wins"] / total * 100) if total else 0
    text = (
        f"Сделок всего: {total} (✅ {st['wins']} / 🔻 {st['losses']}, "
        f"winrate {winrate:.0f}%)\n"
        f"Суммарный PnL: {st['total_pnl_usd']:+.2f}$"
    )
    if st.get("initial_balance"):
        text += f" ({st['total_pnl_usd'] / st['initial_balance'] * 100:+.2f}% от стартового депо)"
    recent = STATE.trade_history[-5:]
    if recent:
        text += "\n\nПоследние сделки:"
        for t in reversed(recent):
            text += (
                f"\n{t['symbol']} {t['direction']} "
                f"{t['pnl_pct_depo']:+.2f}% / {t['pnl_usd']:+.2f}$ ({t['reason']})"
            )
    return text


# ----------------------------------------------------------------------
# Команды
# ----------------------------------------------------------------------

@dp.message(Command("start", "help"))
async def cmd_help(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await msg.answer(
        "Команды:\n"
        "/status — состояние бота (+кнопки)\n"
        "/start_trading — запустить торговлю\n"
        "/stop_trading — остановить торговлю\n"
        "/balance — баланс с биржи\n"
        "/risk <число> — % депозита при стопе (сейчас "
        f"{STATE.settings['stop_loss_pct']}%)\n"
        "/spread <число> — порог входа в % (сейчас "
        f"{STATE.settings['entry_threshold']}%)\n"
        "/size <число> — % депозита в маржу (сейчас "
        f"{STATE.settings['position_pct']}%)\n"
        "/cooldown <мин> <макс> — пауза между сделками, сек\n"
        "/pairs — список торгуемых zero-fee пар\n"
        "/stats — статистика\n"
        "/close — закрыть текущую позицию"
    )


@dp.message(Command("status"))
async def cmd_status(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await msg.answer(_status_text(), reply_markup=_status_keyboard())


@dp.message(Command("start_trading"))
async def cmd_start_trading(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    STATE.set_setting("trading_enabled", True)
    await msg.answer("🟢 Торговля запущена")


@dp.message(Command("stop_trading"))
async def cmd_stop_trading(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    STATE.set_setting("trading_enabled", False)
    text = "🔴 Торговля остановлена"
    if STATE.open_position:
        text += (
            f"\nОткрыта позиция {STATE.open_position['symbol']} — она будет доведена "
            f"до TP/SL/таймаута. Закрыть сейчас: /close"
        )
    await msg.answer(text)


@dp.message(Command("balance"))
async def cmd_balance(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    try:
        b = await MEXC.usdt_balance()
        await msg.answer(
            f"Баланс USDT (фьючерсы MEXC):\n"
            f"Equity: {float(b.get('equity', 0)):.2f}$\n"
            f"Доступно: {float(b.get('availableBalance', 0)):.2f}$"
        )
    except Exception as e:
        await msg.answer(f"⚠️ Не удалось получить баланс: {e}")


def _parse_float(command: CommandObject) -> float | None:
    try:
        return float(command.args.strip().replace(",", "."))
    except (AttributeError, ValueError):
        return None


@dp.message(Command("risk"))
async def cmd_risk(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    val = _parse_float(command)
    if val is None or not 0.1 <= val <= 50:
        await msg.answer("Использование: /risk 3  (от 0.1 до 50, % депозита при стопе)")
        return
    STATE.set_setting("stop_loss_pct", val)
    await msg.answer(f"✅ Стоп-лосс: {val}% от депозита")


@dp.message(Command("spread"))
async def cmd_spread(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    val = _parse_float(command)
    if val is None or not 0.05 <= val <= 10:
        await msg.answer("Использование: /spread 0.3  (от 0.05 до 10, % спреда для входа)")
        return
    STATE.set_setting("entry_threshold", val)
    await msg.answer(f"✅ Порог входа: спред ≥ {val}%")


@dp.message(Command("size"))
async def cmd_size(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    val = _parse_float(command)
    if val is None or not 1 <= val <= 100:
        await msg.answer("Использование: /size 20  (от 1 до 100, % депозита в маржу)")
        return
    STATE.set_setting("position_pct", val)
    await msg.answer(f"✅ Маржа на сделку: {val}% депозита")


@dp.message(Command("cooldown"))
async def cmd_cooldown(msg: Message, command: CommandObject):
    if not _is_admin(msg.from_user.id):
        return
    try:
        lo, hi = (int(x) for x in command.args.split())
        assert 5 <= lo <= hi <= 3600
    except Exception:
        await msg.answer("Использование: /cooldown 30 120  (мин и макс пауза в секундах)")
        return
    STATE.set_setting("cooldown_min_sec", lo)
    STATE.set_setting("cooldown_max_sec", hi)
    await msg.answer(f"✅ Кулдаун между сделками: {lo}–{hi} сек")


@dp.message(Command("pairs"))
async def cmd_pairs(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    try:
        await ENGINE.refresh_pairs(force=True)
    except Exception as e:
        await msg.answer(f"⚠️ Ошибка обновления пар: {e}")
        return
    if not ENGINE.pairs:
        await msg.answer("После фильтров (zero-fee + Binance + ликвидность) пар не осталось.")
        return
    names = [c["symbol"] for c in ENGINE.pairs]
    text = f"Торгуемых пар: {len(names)}\n" + ", ".join(names)
    await msg.answer(text[:4000])


@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    await msg.answer(_stats_text())


@dp.message(Command("close"))
async def cmd_close(msg: Message):
    if not _is_admin(msg.from_user.id):
        return
    if not STATE.open_position:
        await msg.answer("Открытых позиций нет.")
        return
    await msg.answer(f"Закрываю {STATE.open_position['symbol']}...")
    await TRADER.close_position(reason="закрыто вручную через Telegram")


# ----------------------------------------------------------------------
# Инлайн-кнопки
# ----------------------------------------------------------------------

@dp.callback_query(F.data.in_({"start", "stop", "stats", "close"}))
async def on_button(cb: CallbackQuery):
    if not _is_admin(cb.from_user.id):
        await cb.answer()
        return
    if cb.data == "start":
        STATE.set_setting("trading_enabled", True)
        await cb.message.edit_text(_status_text(), reply_markup=_status_keyboard())
        await cb.answer("Торговля запущена")
    elif cb.data == "stop":
        STATE.set_setting("trading_enabled", False)
        await cb.message.edit_text(_status_text(), reply_markup=_status_keyboard())
        await cb.answer("Торговля остановлена")
    elif cb.data == "stats":
        await cb.message.answer(_stats_text())
        await cb.answer()
    elif cb.data == "close":
        await cb.answer("Закрываю позицию...")
        await TRADER.close_position(reason="закрыто вручную (кнопка)")


async def run_telegram():
    if not bot:
        log.error("TELEGRAM_BOT_TOKEN не задан — Telegram-бот не запущен")
        return
    await dp.start_polling(bot)
