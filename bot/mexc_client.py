"""Клиент MEXC Futures — полностью на WEB-токене, без официальных API-ключей.

Публичные данные (контракты, тикеры, стаканы) — открытые эндпоинты contract.mexc.com.
Все приватные операции (баланс, позиции, плечо, ордера, zero-fee пары) —
внутренние эндпоинты futures.mexc.com с авторизацией по WEB-токену
(cookie "u_id" из браузера, начинается с "WEB...").
"""
import asyncio
import hashlib
import json
import logging
import time

import aiohttp

from config import CREDS, MEXC_CONTRACT_BASE, MEXC_FUTURES_WEB_BASE

log = logging.getLogger("mexc")


class MexcError(Exception):
    pass


class MexcAuthError(MexcError):
    """WEB-токен протух или невалиден."""


class MexcClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # Публичные данные (без авторизации)
    # ------------------------------------------------------------------

    async def _public(self, path: str, params: dict | None = None, retries: int = 3):
        s = await self.session()
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                async with s.get(MEXC_CONTRACT_BASE + path, params=params or {}) as resp:
                    data = await resp.json(content_type=None)
                    if not data.get("success", False) and data.get("code") not in (0, 200):
                        raise MexcError(f"{path}: {data}")
                    return data.get("data", data)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise MexcError(f"{path}: network failure: {last_exc}")

    async def contract_details(self) -> list[dict]:
        """Все фьючерсные контракты MEXC (maxLeverage, contractSize, шаги цены/объёма)."""
        return await self._public("/api/v1/contract/detail")

    async def all_tickers(self) -> list[dict]:
        """Тикеры всех контрактов: bid1/ask1/lastPrice/volume24/amount24."""
        return await self._public("/api/v1/contract/ticker")

    async def depth(self, symbol: str) -> dict:
        return await self._public(f"/api/v1/contract/depth/{symbol}", {"limit": 5})

    # ------------------------------------------------------------------
    # WEB-токен: подпись и запросы
    # ------------------------------------------------------------------

    @staticmethod
    def _web_sign(token: str, content: str) -> tuple[str, str]:
        """Схема подписи внутреннего API MEXC:
        key = md5(token + ts)[7:], sign = md5(ts + content + key).
        Для GET content — пустая строка, для POST — JSON-тело.
        """
        ts = str(int(time.time() * 1000))
        key = hashlib.md5((token + ts).encode()).hexdigest()[7:]
        sign = hashlib.md5((ts + content + key).encode()).hexdigest()
        return ts, sign

    async def _web(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        retries: int = 2,
    ) -> dict | list:
        """Приватный запрос через WEB-токен.

        GET  — параметры в query string, подпись по пустому содержимому.
        POST — JSON-тело, подпись по телу.
        """
        if not CREDS.mexc_web_token:
            raise MexcAuthError(
                "MEXC_WEB_TOKEN не задан. Получите его: futures.mexc.com -> F12 -> "
                "Application -> Cookies -> u_id (начинается с WEB...) и добавьте в .env"
            )
        s = await self.session()
        token = CREDS.mexc_web_token
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            if method == "GET":
                body = None
                ts, sign = self._web_sign(token, "")
            else:
                body = json.dumps(payload or {}, separators=(",", ":"))
                ts, sign = self._web_sign(token, body)
            headers = {
                "Content-Type": "application/json",
                "Authorization": token,
                "x-mxc-nonce": ts,
                "x-mxc-sign": sign,
                "Origin": MEXC_FUTURES_WEB_BASE,
                "Referer": MEXC_FUTURES_WEB_BASE + "/exchange",
            }
            try:
                async with s.request(
                    method,
                    MEXC_FUTURES_WEB_BASE + path,
                    params=(payload or {}) if method == "GET" else None,
                    data=body,
                    headers=headers,
                ) as resp:
                    data = await resp.json(content_type=None)
                    if not data.get("success", False) and data.get("code") not in (0, 200):
                        code = data.get("code")
                        if resp.status == 401 or code in (401, 1002, 4001):
                            raise MexcAuthError(
                                "WEB-токен протух или невалиден — обновите MEXC_WEB_TOKEN "
                                f"(ответ биржи: {data})"
                            )
                        raise MexcError(f"WEB {path}: {data}")
                    return data.get("data", data)
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise MexcError(f"WEB {path}: network failure: {last_exc}")

    # ------------------------------------------------------------------
    # Приватные данные
    # ------------------------------------------------------------------

    async def usdt_balance(self) -> dict:
        """{'availableBalance': .., 'equity': ..}"""
        assets = await self._web("GET", "/api/v1/private/account/assets")
        for a in assets:
            if a.get("currency") == "USDT":
                return a
        raise MexcError("USDT-актив не найден на фьючерсном аккаунте")

    async def open_positions(self) -> list[dict]:
        return await self._web("GET", "/api/v1/private/position/open_positions") or []

    async def zero_fee_symbols(self) -> set[str]:
        """Пары с нулевой комиссией для аккаунта. Список зависит от аккаунта.

        Пробуем приватный эндпоинт; при неудаче — фолбэк на публичные ставки
        (takerFeeRate == 0 в contract/detail).
        """
        try:
            data = await self._web(
                "GET", "/api/v1/private/account/contract/zero_fee_rate"
            )
            symbols = set()
            if isinstance(data, dict):
                for key in ("symbols", "zeroFeeSymbols", "list"):
                    if key in data and isinstance(data[key], list):
                        for item in data[key]:
                            symbols.add(item if isinstance(item, str) else item.get("symbol", ""))
            elif isinstance(data, list):
                for item in data:
                    symbols.add(item if isinstance(item, str) else item.get("symbol", ""))
            symbols.discard("")
            if symbols:
                return symbols
        except Exception as e:
            log.warning("zero_fee_rate эндпоинт не сработал: %s", e)

        # фолбэк: публичные ставки комиссии
        details = await self.contract_details()
        return {
            c["symbol"]
            for c in details
            if float(c.get("takerFeeRate", 1)) == 0 and float(c.get("makerFeeRate", 1)) == 0
        }

    # ------------------------------------------------------------------
    # Торговые операции
    # ------------------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: int, position_type: int):
        """position_type: 1 = long, 2 = short. openType 1 = isolated."""
        await self._web(
            "POST",
            "/api/v1/private/position/change_leverage",
            {"openType": 1, "symbol": symbol, "leverage": leverage, "positionType": position_type},
        )

    async def place_order(
        self,
        symbol: str,
        side: int,           # 1=open long, 3=open short, 4=close long, 2=close short
        vol: float,          # объём в контрактах
        leverage: int,
        stop_loss_price: float | None = None,
        take_profit_price: float | None = None,
    ) -> dict:
        """Маркет-ордер (type=5), изолированная маржа (openType=1)."""
        order = {
            "symbol": symbol,
            "side": side,
            "openType": 1,
            "type": 5,
            "vol": vol,
            "leverage": leverage,
        }
        if stop_loss_price:
            order["stopLossPrice"] = stop_loss_price
        if take_profit_price:
            order["takeProfitPrice"] = take_profit_price
        return await self._web("POST", "/api/v1/private/order/submit", order)


MEXC = MexcClient()
