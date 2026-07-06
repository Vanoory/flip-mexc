"""Загрузка окружения и дефолтные настройки бота."""
import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Credentials:
    mexc_web_token: str = os.getenv("MEXC_WEB_TOKEN", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_admin_id: int = int(os.getenv("TELEGRAM_ADMIN_ID", "0"))

    def validate(self) -> list[str]:
        problems = []
        if not self.mexc_web_token:
            problems.append(
                "MEXC_WEB_TOKEN не задан — без него бот не сможет читать баланс "
                "и открывать сделки (futures.mexc.com -> F12 -> Cookies -> u_id)"
            )
        if not self.telegram_bot_token:
            problems.append("TELEGRAM_BOT_TOKEN не задан")
        if not self.telegram_admin_id:
            problems.append("TELEGRAM_ADMIN_ID не задан")
        return problems


# Настройки по умолчанию (все меняются через Telegram и сохраняются в state.json)
DEFAULT_SETTINGS: dict = {
    "trading_enabled": False,        # торговля выключена до /start_trading
    "stop_loss_pct": 5.0,            # % депозита, теряемый при срабатывании стопа
    "entry_threshold": 0.3,          # % спреда для входа
    "exit_threshold": 0.05,          # % спреда для выхода (TP по схождению)
    "min_profit_pct": 0.3,           # мин. ожидаемый профит (% от депозита), иначе не входим
    "position_pct": 20.0,            # % депозита в маржу на сделку
    "cooldown_min_sec": 30,          # мин. пауза после закрытия сделки
    "cooldown_max_sec": 120,         # макс. пауза после закрытия сделки
    "position_timeout_min": 15,      # таймаут позиции в минутах
    "min_stop_distance_pct": 0.15,   # мин. дистанция стопа от цены входа, %
    "slippage_pct": 0.05,            # оценка проскальзывания (%) для фильтра профита
    "min_volume_24h_usd": 500_000,   # мин. 24ч объём на MEXC для пары
    "signal_confirmations": 2,       # сколько подряд сканов спред должен держаться
    "max_leverage_cap": 200,         # верхний предохранитель на плечо
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "bot.log")

MEXC_CONTRACT_BASE = "https://contract.mexc.com"
MEXC_FUTURES_WEB_BASE = "https://futures.mexc.com"
BINANCE_FUTURES_BASE = "https://fapi.binance.com"
BINANCE_SPOT_BASE = "https://api.binance.com"

CREDS = Credentials()
