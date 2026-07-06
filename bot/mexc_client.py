"""Клиент MEXC Futures.

Два режима:
1. Официальный API (HMAC-подпись) — чтение баланса, позиций, контрактов,
   попытка выставления ордеров (эндпоинт годами "на обслуживании", но пробуем).
2. WEB-токен из браузера (cookie u_id) — рабочий способ выставлять
   фьючерсные ордера через внутренние эндпоинты futures.mexc.com.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time

import aiohttp

from config import CREDS, MEXC_CONTRACT_BASE, MEXC_FUTURES_WEB_BASE

log = logging.getLogger("mexc")


class MexcError(Exception):
    pass


class MexcClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._official_orders_ok: bool | None = None  # None = ещё не пробовали

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
    # Официальный API (HMAC)
    # ------------------------------------------------------------------

    def _sign_official(self, param_str: str, ts: str) -> str:
        payload = CREDS.mexc_api_key + ts + param_str
        return hmac.new(
            CREDS.mexc_api_secret.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()

    async def _official(self, method: str, path: str, params: dict | None = None, retries: int = 3):
        params = params or {}
        s = await self.session()
        last_exc: Exception | None = None
        for attempt in range(retries):
            ts = str(int(time.time() * 1000))
            if method == "GET":
                param_str = "&".join(f"{k}={params[k]}" for k in sorted(params))
                sig = self._sign_official(param_str, ts)
                headers = {"ApiKey": CREDS.mexc_api_key, "Request-Time": ts, "Signature": sig}
                url = MEXC_CONTRACT_BASE + path + (("?" + param_str) if param_str else "")
                req = s.get(url, headers=headers)
            else:
                body = json.dumps(params, separators=(",", ":"))
                sig = self._sign_official(body, ts)
                headers = {
                    "ApiKey": CREDS.mexc_api_key,
                    "Request-Time": ts,
                    "Signature": sig,
                    "Content-Type": "application/json",
                }
                req = s.post(MEXC_CONTRACT_BASE + path, data=body, headers=headers)
            try:
                async with req as resp:
                    data = await resp.json(content_type=None)
                    if not data.get("success", False) and data.get("code") not in (0, 200):
                        raise MexcError(f"{path}: {data}")
                    return data.get("data", data)
            except (aiohttp.ClientError, asyncio.TimeoutError, MexcError) as e:
                last_exc = e
                if isinstance(e, MexcError):
                    raise
                await asyncio.sleep(1.5 * (attempt + 1))
        raise MexcError(f"{path}: network failure: {last_exc}")

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

    # ---------- публичные данные ----------

    async def contract_details(self) -> list[dict]:
        """Все фьючерсные контракты MEXC (maxLeverage, contractSize, шаги цены/объёма)."""
        return await self._public("/api/v1/contract/detail")

    async def all_tickers(self) -> list[dict]:
        """Тикеры всех контрактов: bid1/ask1/lastPrice/volume24/amount24."""
        return await self._public("/api/v1/contract/ticker")

    async def depth(self, symbol: str) -> dict:
        return await self._public(f"/api/v1/contract/depth/{symbol}", {"limit": 5})

    # ---------- приватные данные (официальный API) ----------

    async def usdt_balance(self) -> dict:
        """{'availableBalance': .., 'equity': ..}"""
        assets = await self._official("GET", "/api/v1/private/account/assets")
        for a in assets:
            if a.get("currency") == "USDT":
                return a
        raise MexcError("USDT-актив не найден на фьючерсном аккаунте")

    async def open_positions(self) -> list[dict]:
        return await self._official("GET", "/api/v1/private/position/open_positions") or []

    async def zero_fee_symbols(self) -> set[str]:
        """Пары с нулевой комиссией для аккаунта. Список зависит от аккаунта.

        Пробуем приватный эндпоинт; при неудаче — фолбэк на публичные ставки
        (takerFeeRate == 0 в contract/detail).
        """
        try:
            data = await self._official(
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
    # Ордера: официальный API -> фолбэк на WEB-токен
    # ------------------------------------------------------------------

    async def set_leverage(self, symbol: str, leverage: int, position_type: int):
        """position_type: 1 = long, 2 = short. openType 1 = isolated."""
        try:
            await self._official(
                "POST",
                "/api/v1/private/position/change_leverage",
                {"openType": 1, "symbol": symbol, "leverage": leverage, "positionType": position_type},
            )
        except MexcError as e:
            log.warning("change_leverage через API не прошёл (%s), пробуем WEB", e)
            await self._web_request(
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

        # 1) официальный API, если ещё не знаем что он мёртв
        if self._official_orders_ok is not False:
            try:
                result = await self._official("POST", "/api/v1/private/order/submit", order)
                self._official_orders_ok = True
                return result
            except MexcError as e:
                self._official_orders_ok = False
                log.warning("Официальный order/submit не работает (%s) — переходим на WEB-токен", e)

        # 2) WEB-токен
        if not CREDS.mexc_web_token:
            raise MexcError(
                "Официальный эндпоинт ордеров недоступен, а MEXC_WEB_TOKEN не задан. "
                "Добавьте WEB-токен в .env (см. README)."
            )
        return await self._web_request("POST", "/api/v1/private/order/submit", order)

    # ------------------------------------------------------------------
    # WEB-токен режим (внутренние эндпоинты futures.mexc.com)
    # ------------------------------------------------------------------

    @staticmethod
    def _web_sign(token: str, body_json: str) -> tuple[str, str]:
        ts = str(int(time.time() * 1000))
        key = hashlib.md5((token + ts).encode()).hexdigest()[7:]
        sign = hashlib.md5((ts + body_json + key).encode()).hexdigest()
        return ts, sign

    async def _web_request(self, method: str, path: str, payload: dict) -> dict:
        s = await self.session()
        body = json.dumps(payload, separators=(",", ":"))
        ts, sign = self._web_sign(CREDS.mexc_web_token, body)
        headers = {
            "Content-Type": "application/json",
            "Authorization": CREDS.mexc_web_token,
            "x-mxc-nonce": ts,
            "x-mxc-sign": sign,
            "Origin": MEXC_FUTURES_WEB_BASE,
            "Referer": MEXC_FUTURES_WEB_BASE + "/exchange",
        }
        async with s.request(method, MEXC_FUTURES_WEB_BASE + path, data=body, headers=headers) as resp:
            data = await resp.json(content_type=None)
            if not data.get("success", False) and data.get("code") not in (0, 200):
                code = data.get("code")
                if code in (401, 1002, 4001) or resp.status == 401:
                    raise MexcError(
                        "WEB-токен протух или невалиден — обновите MEXC_WEB_TOKEN "
                        f"(ответ биржи: {data})"
                    )
                raise MexcError(f"WEB {path}: {data}")
            return data.get("data", data)


MEXC = MexcClient()
