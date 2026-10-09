"""
Jupiter v6 Swap API Client
==========================
Integrates Jupiter v6 Swap routing, fetching dynamic quotes with volatility-adjusted
slippage and constructing serialized VersionedTransactions for execution.
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional
import aiohttp

from core.logger import get_logger

WSOL_MINT = "So11111111111111111111111111111111111111112"


class JupiterClient:
    """Asynchronous client for Jupiter v6 Swap API."""

    def __init__(self, api_url: str = "https://lite-api.jup.ag/swap/v1"):
        self.endpoints = [
            "https://lite-api.jup.ag/swap/v1",
            "https://api.jup.ag/swap/v1",
        ]
        if api_url.rstrip("/") not in self.endpoints:
            self.endpoints.insert(0, api_url.rstrip("/"))
        self.api_url = self.endpoints[0]
        self.logger = get_logger()
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            conn = aiohttp.TCPConnector(resolver=aiohttp.DefaultResolver())
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4.5),
                connector=conn,
                headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_quote(
        self,
        input_mint: str,
        output_mint: str,
        amount_lamports: int,
        slippage_bps: int = 250,
        only_direct_routes: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetches best swap route quote from Jupiter v6 with multi-endpoint failover.
        """
        session = await self._get_session()
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount_lamports),
            "slippageBps": str(slippage_bps),
            "onlyDirectRoutes": "true" if only_direct_routes else "false",
        }

        for ep in self.endpoints:
            url = f"{ep}/quote"
            try:
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 429:
                        await asyncio.sleep(0.3)
                        continue
            except Exception:
                continue
        return None

    async def build_swap_transaction(
        self,
        quote_response: Dict[str, Any],
        user_public_key: str,
        dynamic_compute_unit_limit: bool = True,
        priority_fee_lamports: Optional[int] = None,
    ) -> Optional[str]:
        """
        Requests serialized VersionedTransaction from Jupiter /swap endpoint with failover.
        """
        session = await self._get_session()
        payload = {
            "quoteResponse": quote_response,
            "userPublicKey": user_public_key,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": dynamic_compute_unit_limit,
            "asLegacyTransaction": False,
        }

        if priority_fee_lamports is not None:
            payload["prioritizationFeeLamports"] = priority_fee_lamports

        for ep in self.endpoints:
            url = f"{ep}/swap"
            try:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("swapTransaction")
                    if resp.status == 429:
                        await asyncio.sleep(0.3)
                        continue
            except Exception:
                continue
        return None
