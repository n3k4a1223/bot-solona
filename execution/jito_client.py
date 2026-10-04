"""
Dynamic Jito MEV Bundle Integration & Adaptive Tip Engine
=========================================================
Interacts with Jito Block Engine JSON-RPC endpoints to submit MEV-protected
atomic transaction bundles, adaptively calculating tips using real-time
cluster congestion and Jito tip floor percentiles.
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional
import aiohttp
from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer

from core.logger import get_logger

# Official Jito Block Engine Tip Accounts
JITO_TIP_ACCOUNTS: List[str] = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x63VTxvQssQn1WNVJFgyDxzs5hmdzzUF5epWkz",
    "Cw8CFyM9FkoMi7K7CrncBg1x54nrJx4SQK462Gvd5Ath",
    "GZDXwwkittNuQJEgpNmTmUudNedTuejjGKnCXMZoWBB",
    "Cad9WecDhGlY4vGfA6a2g6pvh1QhXzT76i2hW1fW9z4",
    "ADuUkR4vqLUMWXxW9gh6D6L8pWHLHzDhe69ApabvnPeR",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]

# Primary and fallback Block Engine URLs
JITO_BLOCK_ENGINES: List[str] = [
    "https://ny.mainnet.block-engine.jito.wtf",
    "https://amsterdam.mainnet.block-engine.jito.wtf",
    "https://frankfurt.mainnet.block-engine.jito.wtf",
    "https://tokyo.mainnet.block-engine.jito.wtf",
]


class JitoMEVClient:
    """
    Submits atomic bundles to Jito Block Engines with dynamic MEV tips.
    Guarantees front-running / sandwich protection without mempool exposure.
    """

    def __init__(
        self,
        block_engine_url: str = "https://ny.mainnet.block-engine.jito.wtf",
        tip_floor_url: str = "https://bundles.jito.wtf/api/v1/bundles/tip_floor",
        min_tip_lamports: int = 100_000,       # 0.0001 SOL
        max_tip_lamports: int = 5_000_000,     # 0.0050 SOL
        default_percentile: int = 75,
    ):
        self.block_engine_url = block_engine_url.rstrip("/")
        self.tip_floor_url = tip_floor_url
        self.min_tip_lamports = min_tip_lamports
        self.max_tip_lamports = max_tip_lamports
        self.default_percentile = default_percentile
        self.logger = get_logger()
        self._session: Optional[aiohttp.ClientSession] = None
        self._cached_tip_floor: Dict[str, float] = {}
        self._last_tip_fetch_time: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            conn = aiohttp.TCPConnector(resolver=aiohttp.DefaultResolver())
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=4.0),
                connector=conn,
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def get_random_tip_account(self) -> Pubkey:
        """Picks a random Jito tip account to evenly disperse validator tips."""
        chosen = random.choice(JITO_TIP_ACCOUNTS)
        return Pubkey.from_string(chosen)

    def create_tip_instruction(self, payer: Pubkey, tip_lamports: int) -> Instruction:
        """
        Creates a native SystemProgram transfer instruction routing
        the tip to a designated Jito tip account.
        """
        tip_recipient = self.get_random_tip_account()
        params = TransferParams(
            from_pubkey=payer,
            to_pubkey=tip_recipient,
            lamports=tip_lamports,
        )
        return transfer(params)

    async def calculate_adaptive_tip(
        self,
        is_high_congestion: bool = False,
        is_exceptional_momentum: bool = False,
    ) -> int:
        """
        Queries Jito dynamic tip floor API and adapts tip based on cluster state:
        - Normal conditions: 75th percentile
        - High congestion / momentum: 95th percentile
        - Clamped strictly between min_tip_lamports and max_tip_lamports
        """
        target_pct = self.default_percentile
        if is_high_congestion or is_exceptional_momentum:
            target_pct = 95

        tip_data = await self._fetch_tip_floor()
        key = f"landed_tips_{target_pct}th_percentile"
        floor_sol = tip_data.get(key, 0.0001)

        # Convert SOL to lamports
        tip_lamports = int(floor_sol * 1_000_000_000)

        # Dynamic clamping
        adaptive_tip = max(self.min_tip_lamports, min(self.max_tip_lamports, tip_lamports))
        return adaptive_tip

    async def _fetch_tip_floor(self) -> Dict[str, float]:
        """Fetches latest tip floor percentiles with 10-second caching."""
        now = time.time()
        if now - self._last_tip_fetch_time < 10.0 and self._cached_tip_floor:
            return self._cached_tip_floor

        session = await self._get_session()
        try:
            async with session.get(self.tip_floor_url, timeout=aiohttp.ClientTimeout(total=2.5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        self._cached_tip_floor = data[0]
                        self._last_tip_fetch_time = now
                        return self._cached_tip_floor
        except Exception:
            pass

        # Fallback values if API is temporarily unreachable
        return {
            "landed_tips_50th_percentile": 0.0001,
            "landed_tips_75th_percentile": 0.00025,
            "landed_tips_95th_percentile": 0.001,
            "landed_tips_99th_percentile": 0.0025,
        }

    async def send_bundle(self, encoded_transactions: List[str]) -> Optional[str]:
        """
        Sends an atomic bundle of base58/base64-encoded signed transactions to Jito.
        """
        session = await self._get_session()
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000) % 1_000_000,
            "method": "sendBundle",
            "params": [encoded_transactions],
        }

        # Try primary and fallback block engines
        for engine_url in JITO_BLOCK_ENGINES:
            url = f"{engine_url}/api/v1/bundles"
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        bundle_id = res.get("result")
                        if bundle_id:
                            return str(bundle_id)
            except Exception:
                continue

        return None
