"""
OKX Exchange v5 High-Frequency Asynchronous Client
==================================================
Provides institutional-grade asynchronous interaction with the OKX v5 API:
- HMAC-SHA256 signature generation and ISO 8601 UTC timestamp synchronization
- Account balance and portfolio equity evaluation
- Multi-asset market data (tickers, orderbook, OHLCV candles)
- Trade order routing (Market, Limit, Stop) with simulated / live environment support
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("okx_client")


class OKXClient:
    """
    Production-grade asynchronous REST client for OKX v5 Exchange API.
    Supports both simulated (demo) trading and live mainnet execution.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        simulated: bool = True,
        rest_url: str = "https://www.okx.com",
        timeout_seconds: float = 8.0,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.simulated = simulated
        self.rest_url = rest_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _get_timestamp(self) -> str:
        """Generate ISO 8601 UTC timestamp with millisecond resolution."""
        return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _generate_signature(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Create Base64-encoded HMAC-SHA256 signature according to OKX v5 protocol."""
        pre_hash = f"{timestamp}{method.upper()}{request_path}{body}"
        mac = hmac.new(
            self.secret_key.encode("utf-8"),
            pre_hash.encode("utf-8"),
            digestmod=hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _build_headers(self, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        """Construct all required OKX v5 authenticated headers."""
        ts = self._get_timestamp()
        sign = self._generate_signature(ts, method, request_path, body)
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        }
        if self.simulated:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute signed HTTP request against OKX v5 endpoint."""
        session = await self._get_session()
        query_str = ""
        if params:
            import urllib.parse
            query_str = "?" + urllib.parse.urlencode(params)
        
        full_path = path + query_str
        url = self.rest_url + full_path
        body_str = json.dumps(data) if data else ""

        headers = self._build_headers(method, full_path, body_str)

        try:
            async with session.request(
                method=method,
                url=url,
                headers=headers,
                data=body_str if data else None,
            ) as response:
                text = await response.text()
                try:
                    payload = json.loads(text)
                except Exception as json_err:
                    logger.error(f"[OKX] Failed to parse JSON response: {text[:200]}")
                    return {"code": str(response.status), "msg": f"Invalid JSON: {json_err}", "data": []}

                if str(payload.get("code")) != "0":
                    logger.warning(f"[OKX API Warning] path={full_path} code={payload.get('code')} msg={payload.get('msg')}")

                return payload
        except asyncio.TimeoutError:
            logger.error(f"[OKX Timeout] {method} {url} timed out after {self.timeout_seconds}s")
            return {"code": "-1", "msg": "Timeout connecting to OKX", "data": []}
        except Exception as e:
            logger.error(f"[OKX Connection Error] {method} {url} error: {e}")
            return {"code": "-1", "msg": str(e), "data": []}

    # -------------------------------------------------------------------------
    # Account & Portfolio
    # -------------------------------------------------------------------------
    async def get_balance(self) -> Dict[str, Any]:
        """Retrieve total portfolio equity and detailed asset balances."""
        res = await self._request("GET", "/api/v5/account/balance")
        if res.get("code") == "0" and res.get("data"):
            acc_data = res["data"][0]
            total_eq_usd = float(acc_data.get("totalEq", 0.0) or 0.0)
            details = []
            for item in acc_data.get("details", []):
                cash_bal = float(item.get("cashBal", 0.0) or 0.0)
                eq_usd = float(item.get("eqUsd", 0.0) or 0.0)
                if cash_bal > 0 or eq_usd > 0.01:
                    details.append({
                        "ccy": item.get("ccy"),
                        "cash_bal": cash_bal,
                        "avail_bal": float(item.get("availBal", 0.0) or 0.0),
                        "eq_usd": eq_usd,
                        "upl": float(item.get("totalPnl", 0.0) or 0.0),
                        "upl_ratio": float(item.get("totalPnlRatio", 0.0) or 0.0),
                    })
            return {
                "success": True,
                "total_eq_usd": total_eq_usd,
                "details": details,
                "raw": acc_data,
            }
        return {"success": False, "total_eq_usd": 0.0, "details": [], "msg": res.get("msg", "Unknown error")}

    async def get_positions(self, inst_type: str = "SWAP") -> List[Dict[str, Any]]:
        """Retrieve active derivative or margin positions."""
        res = await self._request("GET", "/api/v5/account/positions", params={"instType": inst_type})
        if res.get("code") == "0":
            return res.get("data", [])
        return []

    # -------------------------------------------------------------------------
    # Market Data
    # -------------------------------------------------------------------------
    async def get_ticker(self, inst_id: str) -> Dict[str, Any]:
        """Retrieve 24h ticker metrics for a single instrument (e.g. BTC-USDT)."""
        res = await self._request("GET", "/api/v5/market/ticker", params={"instId": inst_id})
        if res.get("code") == "0" and res.get("data"):
            t = res["data"][0]
            return {
                "inst_id": inst_id,
                "last": float(t.get("last", 0.0)),
                "high24h": float(t.get("high24h", 0.0)),
                "low24h": float(t.get("low24h", 0.0)),
                "vol24h": float(t.get("vol24h", 0.0)),
                "volCcy24h": float(t.get("volCcy24h", 0.0)),
                "bid": float(t.get("bidPx", 0.0)),
                "ask": float(t.get("askPx", 0.0)),
                "ts": int(t.get("ts", 0)),
            }
        return {}

    async def get_candles(self, inst_id: str, bar: str = "1m", limit: int = 60) -> List[Dict[str, Any]]:
        """
        Retrieve historical OHLCV candle bars.
        bar values: 1m, 3m, 5m, 15m, 1H, 4H, 1D.
        Returns oldest -> newest list of candles.
        """
        res = await self._request("GET", "/api/v5/market/candles", params={"instId": inst_id, "bar": bar, "limit": str(limit)})
        candles = []
        if res.get("code") == "0" and res.get("data"):
            # OKX returns newest first, reverse so index 0 is oldest
            raw_candles = list(reversed(res["data"]))
            for c in raw_candles:
                candles.append({
                    "ts": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "vol": float(c[5]),
                    "vol_ccy": float(c[6]) if len(c) > 6 else 0.0,
                })
        return candles

    # -------------------------------------------------------------------------
    # Trade Execution
    # -------------------------------------------------------------------------
    async def place_order(
        self,
        inst_id: str,
        side: str,  # "buy" or "sell"
        sz: str,    # Order quantity
        ord_type: str = "market",
        td_mode: str = "cash",
        px: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit a spot or margin trade order to OKX.
        side: 'buy' | 'sell'
        ord_type: 'market' | 'limit'
        td_mode: 'cash' (spot), 'cross', 'isolated'
        """
        body: Dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side.lower(),
            "ordType": ord_type.lower(),
            "sz": str(sz),
        }
        if px and ord_type.lower() == "limit":
            body["px"] = str(px)

        res = await self._request("POST", "/api/v5/trade/order", data=body)
        if res.get("code") == "0" and res.get("data"):
            order_info = res["data"][0]
            logger.info(f"[OKX ORDER SUCCESS] {side.upper()} {sz} {inst_id} -> OrderID: {order_info.get('ordId')}")
            return {
                "success": True,
                "order_id": order_info.get("ordId"),
                "client_id": order_info.get("clOrdId"),
                "sCode": order_info.get("sCode"),
                "sMsg": order_info.get("sMsg"),
            }
        else:
            logger.error(f"[OKX ORDER FAILED] {side.upper()} {inst_id}: {res.get('msg')}")
            return {"success": False, "msg": res.get("msg", "Order placement failed"), "code": res.get("code")}

    async def cancel_order(self, inst_id: str, ord_id: str) -> bool:
        """Cancel an open order."""
        res = await self._request("POST", "/api/v5/trade/cancel-order", data={"instId": inst_id, "ordId": ord_id})
        return res.get("code") == "0"
