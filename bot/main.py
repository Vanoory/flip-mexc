"""Точка входа: запускает фид Binance, торговый цикл и Telegram-бота."""
import asyncio
import logging
import sys

from binance_feed import BINANCE
from config import CREDS, LOG_FILE
from mexc_client import MEXC
from spread_engine import ENGINE
from telegram_bot import notify_admin, run_telegram
from trader import TRADER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("main")


async def main():
    problems = CREDS.validate()
    for p in problems:
        log.warning("Конфигурация: %s", p)
    critical = [p for p in problems if "TELEGRAM" in p or "MEXC_WEB_TOKEN" in p]
    if critical:
        log.error("Критические проблемы конфигурации — заполните .env и перезапустите.")
        sys.exit(1)

    # подготовка данных
    await BINANCE.load_symbol_lists()
    BINANCE.start()
    try:
        await ENGINE.refresh_pairs(force=True)
    except Exception as e:
        log.error("Не удалось загрузить список пар при старте: %s", e)

    TRADER.start(notify=notify_admin)

    await notify_admin(
        "🤖 Бот запущен.\n"
        f"Пар в списке: {len(ENGINE.pairs)}\n"
        "Торговля выключена — включить: /start_trading\n"
        "Все команды: /help"
    )
    if ENGINE.last_refresh_error:
        await notify_admin(f"⚠️ {ENGINE.last_refresh_error} — проверьте /pairs позже.")

    try:
        await run_telegram()
    finally:
        await MEXC.close()
        await BINANCE.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
