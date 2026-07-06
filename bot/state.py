"""Персистентное состояние бота: настройки, статистика, история сделок."""
import json
import os
import threading
import time

from config import DEFAULT_SETTINGS, STATE_FILE

_lock = threading.Lock()


class State:
    def __init__(self):
        self.settings: dict = dict(DEFAULT_SETTINGS)
        self.stats: dict = {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl_usd": 0.0,
            "initial_balance": None,   # фиксируется при первом запуске
            "started_at": time.time(),
        }
        self.trade_history: list[dict] = []   # последние 200 сделок
        self.open_position: dict | None = None
        self.blacklist: dict[str, str] = {}   # symbol -> причина (запрещённые биржей пары)
        self._load()

    # ---------- persistence ----------

    def _load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = data.get("settings", {})
            # мержим с дефолтами — новые ключи получают дефолтные значения
            self.settings = {**DEFAULT_SETTINGS, **saved}
            self.stats = {**self.stats, **data.get("stats", {})}
            self.trade_history = data.get("trade_history", [])
            self.open_position = data.get("open_position")
            self.blacklist = data.get("blacklist", {})
        except Exception:
            pass  # повреждённый файл — начинаем с дефолтов

    def save(self):
        with _lock:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "settings": self.settings,
                        "stats": self.stats,
                        "trade_history": self.trade_history[-200:],
                        "open_position": self.open_position,
                        "blacklist": self.blacklist,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, STATE_FILE)

    # ---------- helpers ----------

    def set_setting(self, key: str, value):
        self.settings[key] = value
        self.save()

    def blacklist_symbol(self, symbol: str, reason: str):
        self.blacklist[symbol] = reason
        self.save()

    def record_trade(self, trade: dict):
        self.stats["total_trades"] += 1
        pnl = trade.get("pnl_usd", 0.0)
        self.stats["total_pnl_usd"] += pnl
        if pnl >= 0:
            self.stats["wins"] += 1
        else:
            self.stats["losses"] += 1
        self.trade_history.append(trade)
        self.trade_history = self.trade_history[-200:]
        self.save()


STATE = State()
