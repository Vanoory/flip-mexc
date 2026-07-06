"""Публичные референсные цены Binance.

Одним REST-запросом забираем bookTicker по ВСЕМ парам (фьючерсы + спот-фолбэк)
раз в ~1.5 секунды — это дёшево по rate-limit и не требует веб-сокетов.
"""
import asyncio
import logging
import time

import aiohttp

from config import BINANCE_FUTURES_BASE, BINANCE_SPOT_BASE

log = logging.getLogger("binance")


class BinanceFeed:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self.futures_symbols: set[str] = set()   # напр. {"BTCUSDT", ...}
        self.spot_symbols: set[str] = set()
        self._prices: dict[str, tuple[float, float]] = {}  # symbol -> (mid, updated_at)
        self._task: asyncio.Task | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self._session

    async def close(self):
        if self._task:
            self._task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    # ---------- каталоги пар ----------

    async def load_symbol_lists(self):
        s = await self.session()
        try:
            async with s.get(BINANCE_FUTURES_BASE + "/fapi/v1/exchangeInfo") as resp:
                data = await resp.json()
            self.futures_symbols = {
                x["symbol"] for x in data.get("symbols", []) if x.get("status") == "TRADING"
            }
        except Exception as e:
            log.error("Не удалось загрузить список фьючерсов Binance: %s", e)
        try:
            async with s.get(
                BINANCE_SPOT_BASE + "/api/v3/exchangeInfo", params={"permissions": "SPOT"}
            ) as resp:
                data = await resp.json()
            self.spot_symbols = {
                x["symbol"] for x in data.get("symbols", []) if x.get("status") == "TRADING"
            }
        except Exception as e:
            log.error("Не удалось загрузить список спота Binance: %s", e)

    def has_symbol(self, binance_symbol: str) -> bool:
        return binance_symbol in self.futures_symbols or binance_symbol in self.spot_symbols

    # ---------- цикл цен ----------

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Ошибка опроса цен Binance: %s", e)
                await asyncio.sleep(5)
            await asyncio.sleep(1.5)

    async def _poll_once(self):
        s = await self.session()
        now = time.time()
        # фьючерсы: один запрос — все пары
        async with s.get(BINANCE_FUTURES_BASE + "/fapi/v1/ticker/bookTicker") as resp:
            for t in await resp.json():
                bid, ask = float(t["bidPrice"]), float(t["askPrice"])
                if bid > 0 and ask > 0:
                    self._prices["F:" + t["symbol"]] = ((bid + ask) / 2, now)
        # спот: один запрос — все пары (фолбэк для пар без фьючерсов)
        async with s.get(BINANCE_SPOT_BASE + "/api/v3/ticker/bookTicker") as resp:
            for t in await resp.json():
                bid, ask = float(t["bidPrice"]), float(t["askPrice"])
                if bid > 0 and ask > 0:
                    self._prices["S:" + t["symbol"]] = ((bid + ask) / 2, now)

    def mid_price(self, binance_symbol: str, max_age_sec: float = 10.0) -> float | None:
        """Приоритет фьючерсной цене, фолбэк на спот. None если цена устарела."""
        for prefix in ("F:", "S:"):
            entry = self._prices.get(prefix + binance_symbol)
            if entry and time.time() - entry[1] <= max_age_sec:
                return entry[0]
        return None


BINANCE = BinanceFeed()
