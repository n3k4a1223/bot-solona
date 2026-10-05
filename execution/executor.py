"""
Unified Execution Pipeline & Transaction Manager
================================================
Coordinates wallet signing (solders), Jupiter routing, Jito MEV bundling,
dynamic priority fee adaptation, and explorer link logging.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from typing import Any, Dict, Optional, Tuple
import aiohttp
import base58
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from execution.jito_client import JitoMEVClient
from execution.jupiter_client import JupiterClient, WSOL_MINT


class TradeExecutor:
    """
    Executes buy and sell orders with sub-second routing via Jupiter and Jito MEV.
    """

    def __init__(
        self,
        rpc_balancer: MultiRPCBalancer,
        jupiter_client: JupiterClient,
        jito_client: JitoMEVClient,
        private_key_str: str = "",
        dry_run: bool = True,
        use_jito: bool = True,
    ):
        self.rpc = rpc_balancer
        self.jupiter = jupiter_client
        self.jito = jito_client
        self.dry_run = dry_run
        self.use_jito = use_jito
        self.logger = get_logger(dry_run=dry_run)

        # Initialize Wallet Keypair
        self.keypair = self._load_keypair(private_key_str)
        self.pubkey_str = str(self.keypair.pubkey())
        self.logger.log_info(f"Wallet Initialized: [bold cyan]{self.pubkey_str}[/]")

    def _load_keypair(self, key_str: str) -> Keypair:
        """Parses private key from Base58 or JSON byte array."""
        if not key_str or key_str.strip() == "" or "your_" in key_str:
            if not self.dry_run:
                raise ValueError("WALLET_PRIVATE_KEY must be provided for live trading.")
            # Ephemeral keypair for simulation mode
            return Keypair()

        key_str = key_str.strip()
        try:
            if key_str.startswith("[") and key_str.endswith("]"):
                byte_list = json.loads(key_str)
                return Keypair.from_bytes(bytes(byte_list))
            else:
                raw_bytes = base58.b58decode(key_str)
                return Keypair.from_bytes(raw_bytes)
        except Exception as e:
            if self.dry_run:
                self.logger.log_warning(f"Failed to parse private key ({e}). Using simulation keypair.")
                return Keypair()
            raise ValueError(f"Invalid WALLET_PRIVATE_KEY format: {e}")

    async def _execute_pumpportal_buy(
        self,
        token_mint: str,
        amount_sol: float,
        slippage_percent: float = 15.0,
    ) -> Tuple[bool, Optional[str], int]:
        """
        Executes buy order on Pump.fun bonding curve via PumpPortal trade-local API.
        """
        import aiohttp
        payload = {
            "publicKey": self.pubkey_str,
            "action": "buy",
            "mint": token_mint,
            "amount": amount_sol,
            "denominatedInSol": "true",
            "slippage": slippage_percent,
            "priorityFee": 0.0005,
            "pool": "pump" if token_mint.endswith("pump") else "auto",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8.0)) as session:
                async with session.post("https://pumpportal.fun/api/trade-local", json=payload) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        self.logger.log_error(f"PumpPortal buy returned status {resp.status}: {err_text}")
                        return False, None, 0
                    raw_tx_bytes = await resp.read()

            tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            signed_b64 = base64.b64encode(bytes(signed_tx)).decode("ascii")
            signature_str = str(signed_tx.signatures[0])

            # Attempt submission via Jito MEV Bundle
            if self.use_jito:
                bundle_id = await self.jito.send_bundle([signed_b64])
                if bundle_id:
                    self.logger.log_trade(
                        action="BUY [PUMP/JITO]",
                        token_mint=token_mint,
                        amount_sol=amount_sol,
                        price_sol=0.0,
                        signature=signature_str,
                        notes=f"PumpPortal Jito Bundle: {bundle_id[:12]}",
                    )
                    expected_tokens = int(amount_sol * 1_000_000_000)
                    return True, signature_str, expected_tokens

            # Direct RPC submission
            tx_sig = await self.rpc.send_raw_transaction(signed_b64)
            self.logger.log_trade(
                action="BUY [PUMP/RPC]",
                token_mint=token_mint,
                amount_sol=amount_sol,
                price_sol=0.0,
                signature=tx_sig or signature_str,
                notes="PumpPortal Direct Transaction",
            )
            expected_tokens = int(amount_sol * 1_000_000_000)
            return True, tx_sig or signature_str, expected_tokens
        except Exception as e:
            self.logger.log_error(f"PumpPortal buy execution error: {e}")
            return False, None, 0

    async def _execute_pumpportal_sell(
        self,
        token_mint: str,
        tokens_amount: int,
        reason: str = "EXIT",
        slippage_percent: float = 15.0,
    ) -> Tuple[bool, Optional[str], float]:
        """
        Executes sell order on Pump.fun bonding curve via PumpPortal trade-local API.
        """
        import aiohttp
        payload = {
            "publicKey": self.pubkey_str,
            "action": "sell",
            "mint": token_mint,
            "amount": "100%",
            "denominatedInSol": "false",
            "slippage": slippage_percent,
            "priorityFee": 0.0005,
            "pool": "pump" if token_mint.endswith("pump") else "auto",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8.0)) as session:
                async with session.post("https://pumpportal.fun/api/trade-local", json=payload) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        self.logger.log_error(f"PumpPortal sell returned status {resp.status}: {err_text}")
                        return False, None, 0.0
                    raw_tx_bytes = await resp.read()

            tx = VersionedTransaction.from_bytes(raw_tx_bytes)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])
            signed_b64 = base64.b64encode(bytes(signed_tx)).decode("ascii")
            signature_str = str(signed_tx.signatures[0])

            bal_before = await self.rpc.get_balance(self.pubkey_str)

            if self.use_jito:
                bundle_id = await self.jito.send_bundle([signed_b64])
                if bundle_id:
                    await asyncio.sleep(1.5)
                    bal_after = await self.rpc.get_balance(self.pubkey_str)
                    sol_received = max(0.001, bal_after - bal_before)
                    self.logger.log_trade(
                        action=f"SELL [{reason}-PUMP/JITO]",
                        token_mint=token_mint,
                        amount_sol=sol_received,
                        price_sol=0.0,
                        signature=signature_str,
                        notes=f"Closed PumpPortal via Jito: {bundle_id[:12]} | Net SOL: {sol_received:.4f}",
                    )
                    return True, signature_str, sol_received

            tx_sig = await self.rpc.send_raw_transaction(signed_b64)
            await asyncio.sleep(1.5)
            bal_after = await self.rpc.get_balance(self.pubkey_str)
            sol_received = max(0.001, bal_after - bal_before) if bal_after > bal_before else 0.040
            self.logger.log_trade(
                action=f"SELL [{reason}-PUMP/RPC]",
                token_mint=token_mint,
                amount_sol=sol_received,
                price_sol=0.0,
                signature=tx_sig or signature_str,
                notes=f"Closed PumpPortal via Direct RPC | Net SOL: {sol_received:.4f}",
            )
            return True, tx_sig or signature_str, sol_received
        except Exception as e:
            self.logger.log_error(f"PumpPortal sell execution error: {e}")
            return False, None, 0.0

    async def execute_buy(
        self,
        token_mint: str,
        amount_sol: float,
        slippage_bps: int = 250,
        is_congested: bool = False,
        is_exceptional_momentum: bool = False,
    ) -> Tuple[bool, Optional[str], int]:
        """
        Executes a BUY swap: WSOL -> Target Token.
        Returns: (success: bool, tx_signature_or_bundle_id: str, tokens_acquired: int)
        """
        # If token ends with 'pump', route directly via PumpPortal bonding curve
        if token_mint.endswith("pump"):
            if self.dry_run:
                simulated_sig = self._generate_simulated_hash("BUY", token_mint, amount_sol)
                expected_tokens = int(amount_sol * 1_000_000_000)
                self.logger.log_trade(
                    action="BUY [PUMP/SIM]",
                    token_mint=token_mint,
                    amount_sol=amount_sol,
                    price_sol=0.0,
                    signature=simulated_sig,
                    notes=f"Expected: {expected_tokens} tokens",
                )
                return True, simulated_sig, expected_tokens
            return await self._execute_pumpportal_buy(token_mint, amount_sol)

        amount_lamports = int(amount_sol * 1_000_000_000)

        # ---------------------------------------------------------------------
        # 1. Obtain Quote from Jupiter v6
        # ---------------------------------------------------------------------
        quote = await self.jupiter.get_quote(
            input_mint=WSOL_MINT,
            output_mint=token_mint,
            amount_lamports=amount_lamports,
            slippage_bps=slippage_bps,
        )
        if not quote:
            if self.dry_run:
                expected_tokens = int(amount_sol * 1_000_000_000)
            else:
                self.logger.log_info(f"No Jupiter route for {token_mint[:8]}, routing through PumpPortal...")
                return await self._execute_pumpportal_buy(token_mint, amount_sol)
        else:
            expected_tokens = int(quote.get("outAmount", 0))

        # ---------------------------------------------------------------------
        # 2. Dry Run Simulation Mode
        # ---------------------------------------------------------------------
        if self.dry_run:
            simulated_sig = self._generate_simulated_hash("BUY", token_mint, amount_sol)
            self.logger.log_trade(
                action="BUY [SIM]",
                token_mint=token_mint,
                amount_sol=amount_sol,
                price_sol=(amount_sol / (expected_tokens / 1e6)) if expected_tokens > 0 else 0.0,
                signature=simulated_sig,
                notes=f"Expected: {expected_tokens} tokens",
            )
            return True, simulated_sig, expected_tokens

        # ---------------------------------------------------------------------
        # 3. Live Execution: Build, Sign, and Submit
        # ---------------------------------------------------------------------
        tip_lamports = await self.jito.calculate_adaptive_tip(
            is_high_congestion=is_congested,
            is_exceptional_momentum=is_exceptional_momentum,
        )

        swap_tx_b64 = await self.jupiter.build_swap_transaction(
            quote_response=quote,
            user_public_key=self.pubkey_str,
            dynamic_compute_unit_limit=True,
            priority_fee_lamports=tip_lamports if not self.use_jito else None,
        )
        if not swap_tx_b64:
            return False, None, 0

        # Sign VersionedTransaction
        signed_tx_b64, signature_str = self._sign_transaction(swap_tx_b64)

        # Dispatch via Jito MEV Bundle or Direct RPC
        if self.use_jito:
            bundle_id = await self.jito.send_bundle([signed_tx_b64])
            if bundle_id:
                self.logger.log_trade(
                    action="BUY [JITO]",
                    token_mint=token_mint,
                    amount_sol=amount_sol,
                    price_sol=(amount_sol / (expected_tokens / 1e6)) if expected_tokens > 0 else 0.0,
                    signature=signature_str,
                    notes=f"Jito Bundle: {bundle_id[:12]} | Tip: {tip_lamports} lamports",
                )
                return True, signature_str, expected_tokens
            self.logger.log_warning("Jito bundle submission failed; falling back to direct RPC submission.")

        tx_sig = await self.rpc.send_raw_transaction(signed_tx_b64)
        self.logger.log_trade(
            action="BUY [RPC]",
            token_mint=token_mint,
            amount_sol=amount_sol,
            price_sol=(amount_sol / (expected_tokens / 1e6)) if expected_tokens > 0 else 0.0,
            signature=tx_sig,
        )
        return True, tx_sig, expected_tokens

    async def execute_sell(
        self,
        token_mint: str,
        tokens_amount: int,
        reason: str = "EXIT",
        slippage_bps: int = 250,
        is_congested: bool = False,
    ) -> Tuple[bool, Optional[str], float]:
        """
        Executes a SELL swap: Target Token -> WSOL.
        Returns: (success: bool, tx_signature: str, sol_received: float)
        """
        if tokens_amount <= 0:
            return False, None, 0.0

        if token_mint.endswith("pump"):
            if self.dry_run:
                sol_received = 0.08
                simulated_sig = self._generate_simulated_hash("SELL", token_mint, sol_received)
                self.logger.log_trade(
                    action=f"SELL [{reason}-SIM]",
                    token_mint=token_mint,
                    amount_sol=sol_received,
                    price_sol=0.0,
                    signature=simulated_sig,
                    notes=f"Closed {tokens_amount} tokens",
                )
                return True, simulated_sig, sol_received
            return await self._execute_pumpportal_sell(token_mint, tokens_amount, reason=reason)

        quote = await self.jupiter.get_quote(
            input_mint=token_mint,
            output_mint=WSOL_MINT,
            amount_lamports=tokens_amount,
            slippage_bps=slippage_bps,
        )
        if not quote:
            if self.dry_run:
                sol_received = (tokens_amount / 1_000_000_000.0) * 1.08
            else:
                self.logger.log_info(f"No Jupiter quote to sell {token_mint[:8]}, routing through PumpPortal...")
                return await self._execute_pumpportal_sell(token_mint, tokens_amount, reason=reason)
        else:
            out_lamports = int(quote.get("outAmount", 0))
            sol_received = out_lamports / 1_000_000_000.0

        if self.dry_run:
            simulated_sig = self._generate_simulated_hash("SELL", token_mint, sol_received)
            self.logger.log_trade(
                action=f"SELL [{reason}]",
                token_mint=token_mint,
                amount_sol=sol_received,
                price_sol=sol_received / (tokens_amount / 1e6) if tokens_amount > 0 else 0.0,
                signature=simulated_sig,
                notes=f"Closed {tokens_amount} tokens",
            )
            return True, simulated_sig, sol_received

        # Live Execution
        tip_lamports = await self.jito.calculate_adaptive_tip(is_high_congestion=is_congested)
        swap_tx_b64 = await self.jupiter.build_swap_transaction(
            quote_response=quote,
            user_public_key=self.pubkey_str,
            dynamic_compute_unit_limit=True,
            priority_fee_lamports=tip_lamports if not self.use_jito else None,
        )
        if not swap_tx_b64:
            return False, None, 0.0

        signed_tx_b64, signature_str = self._sign_transaction(swap_tx_b64)

        if self.use_jito:
            bundle_id = await self.jito.send_bundle([signed_tx_b64])
            if bundle_id:
                self.logger.log_trade(
                    action=f"SELL [{reason}]",
                    token_mint=token_mint,
                    amount_sol=sol_received,
                    price_sol=sol_received / (tokens_amount / 1e6) if tokens_amount > 0 else 0.0,
                    signature=signature_str,
                    notes=f"Jito Bundle: {bundle_id[:12]}",
                )
                return True, signature_str, sol_received

        tx_sig = await self.rpc.send_raw_transaction(signed_tx_b64)
        self.logger.log_trade(
            action=f"SELL [{reason}]",
            token_mint=token_mint,
            amount_sol=sol_received,
            price_sol=sol_received / (tokens_amount / 1e6) if tokens_amount > 0 else 0.0,
            signature=tx_sig,
        )
        return True, tx_sig, sol_received

    def _sign_transaction(self, tx_b64: str) -> Tuple[str, str]:
        """Deserializes base64 VersionedTransaction, signs it, and returns base64 string."""
        raw_bytes = base64.b64decode(tx_b64)
        tx = VersionedTransaction.from_bytes(raw_bytes)
        signed_tx = VersionedTransaction(tx.message, [self.keypair])
        signed_bytes = bytes(signed_tx)
        signed_b64 = base64.b64encode(signed_bytes).decode("ascii")
        sig_str = str(signed_tx.signatures[0])
        return signed_b64, sig_str

    def _generate_simulated_hash(self, action: str, mint: str, amount: float) -> str:
        """Generates a realistic-looking transaction signature for paper trading."""
        seed = f"{action}-{mint}-{amount}-{time.time()}"
        digest = hashlib.sha256(seed.encode()).digest()
        return base58.b58encode(digest + digest[:32]).decode("ascii")[:88]
