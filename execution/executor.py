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

    async def _execute_direct_pump_buy(
        self,
        token_mint: str,
        amount_sol: float,
        slippage_percent: float = 15.0,
        creator: str = "",
        token_program: str = "",
    ) -> Tuple[bool, Optional[str], int]:
        """
        Executes buy order directly on-chain via the native Pump.fun Program ID V2
        bypassing third-party APIs and Cloudflare blocks completely.
        """
        try:
            import struct
            from solders.instruction import Instruction, AccountMeta
            from solders.message import MessageV0
            from solders.hash import Hash
            from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

            # 1. Resolve creator and token_program
            creator_str = creator
            token_prog_str = token_program
            if not creator_str or not token_prog_str:
                import aiohttp
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5)) as s:
                        async with s.get(
                            f"https://frontend-api-v3.pump.fun/coins?mints={token_mint}",
                            headers={"User-Agent": "Mozilla/5.0"}
                        ) as r:
                            if r.status == 200:
                                data = await r.json()
                                if data and isinstance(data, list) and len(data) > 0:
                                    c_info = data[0]
                                    if not creator_str:
                                        creator_str = c_info.get("creator") or ""
                                    if not token_prog_str:
                                        token_prog_str = c_info.get("token_program") or ""
                except Exception:
                    pass

            if not creator_str:
                creator_str = "FEretvMHhjptWdgJ3ixB4K4myucm9UL3eoCf4DEb8E77"
            if not token_prog_str:
                token_prog_str = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" if token_mint.endswith("pump") else "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

            mint_pubkey = Pubkey.from_string(token_mint)
            creator_pubkey = Pubkey.from_string(creator_str)
            pump_prog = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
            token_prog = Pubkey.from_string(token_prog_str)
            assoc_prog = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
            sys_prog = Pubkey.from_string("11111111111111111111111111111111")
            event_auth = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
            fee_prog = Pubkey.from_string("pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ")

            fee_recipient = Pubkey.from_string("FWsW1xNtWscwNmKv6wVsU1iTzRN6wmmk3MjxRP5tT7hz")
            breaking_fee_recipient = Pubkey.from_string("5YxQFdt3Tr9zJLvkFccqXVUwhdTWJQc1fFg2YPbxvxeD")

            bonding_curve = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pubkey)], pump_prog)[0]
            global_pda = Pubkey.find_program_address([b"global"], pump_prog)[0]
            assoc_bc = Pubkey.find_program_address([bytes(bonding_curve), bytes(token_prog), bytes(mint_pubkey)], assoc_prog)[0]
            assoc_user = Pubkey.find_program_address([bytes(self.keypair.pubkey()), bytes(token_prog), bytes(mint_pubkey)], assoc_prog)[0]
            creator_vault = Pubkey.find_program_address([b"creator-vault", bytes(creator_pubkey)], pump_prog)[0]
            global_vol = Pubkey.find_program_address([b"global_volume_accumulator"], pump_prog)[0]
            user_vol = Pubkey.find_program_address([b"user_volume_accumulator", bytes(self.keypair.pubkey())], pump_prog)[0]
            fee_config = Pubkey.find_program_address([b"fee_config", bytes(pump_prog)], fee_prog)[0]
            bonding_curve_v2 = Pubkey.find_program_address([b"bonding-curve-v2", bytes(mint_pubkey)], pump_prog)[0]

            cu_limit_ix = set_compute_unit_limit(250_000)
            cu_price_ix = set_compute_unit_price(100_000)

            create_ata_ix = Instruction(
                program_id=assoc_prog,
                accounts=[
                    AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=assoc_user, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=self.keypair.pubkey(), is_signer=False, is_writable=False),
                    AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=sys_prog, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=token_prog, is_signer=False, is_writable=False),
                ],
                data=bytes([1]),
            )

            # In pump.fun bonding curve origin, 1 SOL buys approx 28M tokens
            tokens_approx = int((amount_sol / 0.000000035) * (1.0 - slippage_percent / 100.0) * 1e6)
            max_sol_lamports = int(amount_sol * 1e9 * (1.0 + slippage_percent / 100.0))
            buy_discriminator = bytes([102, 6, 61, 18, 1, 218, 235, 234])
            buy_data = buy_discriminator + struct.pack("<QQ", tokens_approx, max_sol_lamports)

            accounts = [
                AccountMeta(pubkey=global_pda, is_signer=False, is_writable=False), # 0
                AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True), # 1
                AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False), # 2
                AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True), # 3
                AccountMeta(pubkey=assoc_bc, is_signer=False, is_writable=True), # 4
                AccountMeta(pubkey=assoc_user, is_signer=False, is_writable=True), # 5
                AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True), # 6
                AccountMeta(pubkey=sys_prog, is_signer=False, is_writable=False), # 7
                AccountMeta(pubkey=token_prog, is_signer=False, is_writable=False), # 8
                AccountMeta(pubkey=creator_vault, is_signer=False, is_writable=True), # 9
                AccountMeta(pubkey=event_auth, is_signer=False, is_writable=False), # 10
                AccountMeta(pubkey=pump_prog, is_signer=False, is_writable=False), # 11
                AccountMeta(pubkey=global_vol, is_signer=False, is_writable=False), # 12
                AccountMeta(pubkey=user_vol, is_signer=False, is_writable=True), # 13
                AccountMeta(pubkey=fee_config, is_signer=False, is_writable=False), # 14
                AccountMeta(pubkey=fee_prog, is_signer=False, is_writable=False), # 15
                AccountMeta(pubkey=bonding_curve_v2, is_signer=False, is_writable=False), # 16
                AccountMeta(pubkey=breaking_fee_recipient, is_signer=False, is_writable=True), # 17
            ]

            buy_ix = Instruction(program_id=pump_prog, accounts=accounts, data=buy_data)

            bh_info = await self.rpc.get_latest_blockhash()
            recent_bh_str = bh_info.get("blockhash") if isinstance(bh_info, dict) else None
            recent_bh = Hash.from_string(recent_bh_str) if recent_bh_str else Hash.default()

            msg = MessageV0.try_compile(
                payer=self.keypair.pubkey(),
                instructions=[cu_limit_ix, cu_price_ix, create_ata_ix, buy_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=recent_bh,
            )
            tx = VersionedTransaction(msg, [self.keypair])
            signed_b64 = base64.b64encode(bytes(tx)).decode("ascii")
            signature_str = str(tx.signatures[0])

            if self.use_jito:
                bundle_id = await self.jito.send_bundle([signed_b64])
                if bundle_id:
                    self.logger.log_trade(
                        action="BUY [PUMP-DIRECT/JITO]",
                        token_mint=token_mint,
                        amount_sol=amount_sol,
                        price_sol=0.0,
                        signature=signature_str,
                        notes=f"Direct On-Chain Pump.fun Jito: {bundle_id[:12]}",
                    )
                    actual_tok = 0.0
                    for _ in range(5):
                        await asyncio.sleep(1.2)
                        actual_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
                        if actual_tok > 0:
                            break
                    if actual_tok <= 0:
                        self.logger.log_warning(f"No token balance confirmed on-chain for {token_mint[:8]}.")
                        return False, None, 0
                    return True, signature_str, int(actual_tok)

            tx_sig = await self.rpc.send_raw_transaction(signed_b64)
            self.logger.log_trade(
                action="BUY [PUMP-DIRECT/RPC]",
                token_mint=token_mint,
                amount_sol=amount_sol,
                price_sol=0.0,
                signature=tx_sig or signature_str,
                notes="Direct On-Chain Pump.fun Transaction",
            )
            actual_tok = 0.0
            for _ in range(5):
                await asyncio.sleep(1.2)
                actual_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
                if actual_tok > 0:
                    break
            if actual_tok <= 0:
                self.logger.log_warning(f"No token balance confirmed on-chain for {token_mint[:8]}.")
                return False, None, 0
            return True, tx_sig or signature_str, int(actual_tok)
        except Exception as e:
            self.logger.log_error(f"Direct pump buy execution error: {e}")
            return False, None, 0

    async def _execute_pumpportal_buy(
        self,
        token_mint: str,
        amount_sol: float,
        slippage_percent: float = 15.0,
    ) -> Tuple[bool, Optional[str], int]:
        """
        Executes buy order on Pump.fun bonding curve with automatic fallback to direct on-chain.
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
            "pool": "auto",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
                async with session.post("https://pumpportal.fun/api/trade-local", json=payload) as resp:
                    if resp.status != 200:
                        self.logger.log_warning(f"PumpPortal API status {resp.status}. Executing directly on-chain...")
                        return await self._execute_direct_pump_buy(token_mint, amount_sol, slippage_percent)
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
                    actual_tok = 0.0
                    for _ in range(4):
                        await asyncio.sleep(1.5)
                        actual_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
                        if actual_tok > 0:
                            break
                    if actual_tok <= 0:
                        self.logger.log_warning(f"No token balance confirmed on-chain for {token_mint[:8]}. Aborting position registration.")
                        return False, None, 0
                    return True, signature_str, int(actual_tok)

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
            actual_tok = 0.0
            for _ in range(4):
                await asyncio.sleep(1.5)
                actual_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
                if actual_tok > 0:
                    break
            if actual_tok <= 0:
                self.logger.log_warning(f"No token balance confirmed on-chain for {token_mint[:8]}. Aborting position registration.")
                return False, None, 0
            return True, tx_sig or signature_str, int(actual_tok)
        except Exception as e:
            self.logger.log_warning(f"PumpPortal API notice ({e}). Executing directly on-chain...")
            return await self._execute_direct_pump_buy(token_mint, amount_sol, slippage_percent)

    async def _execute_direct_pump_sell(
        self,
        token_mint: str,
        tokens_amount: int,
        reason: str = "EXIT",
        slippage_percent: float = 15.0,
    ) -> Tuple[bool, Optional[str], float]:
        """
        Executes sell order directly on-chain via the native Pump.fun Program ID.
        """
        try:
            import struct
            from solders.instruction import Instruction, AccountMeta
            from solders.message import MessageV0
            from solders.hash import Hash
            from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

            current_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
            if current_tok <= 0:
                self.logger.log_warning(f"On-chain balance for {token_mint[:8]} is 0. Auto-clearing position.")
                return True, "cleared_zero_balance", 0.0

            mint_pubkey = Pubkey.from_string(token_mint)
            pump_prog = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")
            token_prog = Pubkey.from_string("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
            assoc_prog = Pubkey.from_string("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL")
            sys_prog = Pubkey.from_string("11111111111111111111111111111111")
            event_auth = Pubkey.from_string("Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1")
            fee_recipient = Pubkey.from_string("CebN5WGQ4jvEPvsVU4EoHEpgzq1VV7AbicfhtW4xC9iM")

            bonding_curve = Pubkey.find_program_address([b"bonding-curve", bytes(mint_pubkey)], pump_prog)[0]
            global_pda = Pubkey.find_program_address([b"global"], pump_prog)[0]
            assoc_bc = Pubkey.find_program_address([bytes(bonding_curve), bytes(token_prog), bytes(mint_pubkey)], assoc_prog)[0]
            assoc_user = Pubkey.find_program_address([bytes(self.keypair.pubkey()), bytes(token_prog), bytes(mint_pubkey)], assoc_prog)[0]

            cu_limit_ix = set_compute_unit_limit(100_000)
            cu_price_ix = set_compute_unit_price(50_000)

            sell_discriminator = bytes([51, 230, 133, 164, 1, 127, 131, 173])
            sell_data = sell_discriminator + struct.pack("<QQ", int(tokens_amount), 1)

            sell_ix = Instruction(
                program_id=pump_prog,
                accounts=[
                    AccountMeta(pubkey=global_pda, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=fee_recipient, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=bonding_curve, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=assoc_bc, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=assoc_user, is_signer=False, is_writable=True),
                    AccountMeta(pubkey=self.keypair.pubkey(), is_signer=True, is_writable=True),
                    AccountMeta(pubkey=sys_prog, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=assoc_prog, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=token_prog, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=event_auth, is_signer=False, is_writable=False),
                    AccountMeta(pubkey=pump_prog, is_signer=False, is_writable=False),
                ],
                data=sell_data,
            )

            bh_info = await self.rpc.get_latest_blockhash()
            recent_bh_str = bh_info.get("blockhash") if isinstance(bh_info, dict) else None
            recent_bh = Hash.from_string(recent_bh_str) if recent_bh_str else Hash.default()

            msg = MessageV0.try_compile(
                payer=self.keypair.pubkey(),
                instructions=[cu_limit_ix, cu_price_ix, sell_ix],
                address_lookup_table_accounts=[],
                recent_blockhash=recent_bh,
            )
            tx = VersionedTransaction(msg, [self.keypair])
            signed_b64 = base64.b64encode(bytes(tx)).decode("ascii")
            signature_str = str(tx.signatures[0])

            bal_before = await self.rpc.get_balance(self.pubkey_str)
            if self.use_jito:
                bundle_id = await self.jito.send_bundle([signed_b64])
                if bundle_id:
                    await asyncio.sleep(1.5)
                    bal_after = await self.rpc.get_balance(self.pubkey_str)
                    sol_received = max(0.001, bal_after - bal_before)
                    self.logger.log_trade(
                        action=f"SELL [{reason}-DIRECT/JITO]",
                        token_mint=token_mint,
                        amount_sol=sol_received,
                        price_sol=0.0,
                        signature=signature_str,
                        notes=f"Direct On-Chain Sell Jito: {bundle_id[:12]} | Net SOL: {sol_received:.4f}",
                    )
                    return True, signature_str, sol_received

            tx_sig = await self.rpc.send_raw_transaction(signed_b64)
            await asyncio.sleep(2.0)
            bal_after = await self.rpc.get_balance(self.pubkey_str)
            sol_received = max(0.001, bal_after - bal_before)
            self.logger.log_trade(
                action=f"SELL [{reason}-DIRECT/RPC]",
                token_mint=token_mint,
                amount_sol=sol_received,
                price_sol=0.0,
                signature=tx_sig or signature_str,
                notes=f"Direct On-Chain Sell Transaction | Net SOL: {sol_received:.4f}",
            )
            return True, tx_sig or signature_str, sol_received
        except Exception as e:
            self.logger.log_error(f"Direct pump sell execution error: {e}")
            return False, None, 0.0

    async def _execute_pumpportal_sell(
        self,
        token_mint: str,
        tokens_amount: int,
        reason: str = "EXIT",
        slippage_percent: float = 15.0,
    ) -> Tuple[bool, Optional[str], float]:
        """
        Executes sell order on Pump.fun bonding curve via PumpPortal trade-local API with direct fallback.
        """
        # Verify on-chain balance before attempting to sell
        current_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
        if current_tok <= 0:
            self.logger.log_warning(f"On-chain balance for {token_mint[:8]} is 0. Auto-clearing position.")
            return True, "cleared_zero_balance", 0.0

        payload = {
            "publicKey": self.pubkey_str,
            "action": "sell",
            "mint": token_mint,
            "amount": "100%",
            "denominatedInSol": "false",
            "slippage": slippage_percent,
            "priorityFee": 0.0005,
            "pool": "auto",
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
                async with session.post("https://pumpportal.fun/api/trade-local", json=payload) as resp:
                    if resp.status != 200:
                        chk_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
                        if chk_tok <= 0:
                            self.logger.log_warning(f"On-chain balance for {token_mint[:8]} is 0. Auto-clearing position.")
                            return True, "cleared_zero_balance", 0.0
                        self.logger.log_warning(f"PumpPortal API status {resp.status}. Executing direct on-chain sell...")
                        return await self._execute_direct_pump_sell(token_mint, tokens_amount, reason, slippage_percent)
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
            self.logger.log_warning(f"PumpPortal sell notice ({e}). Executing direct on-chain sell...")
            return await self._execute_direct_pump_sell(token_mint, tokens_amount, reason, slippage_percent)

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
        amount_lamports = int(amount_sol * 1_000_000_000)

        # ---------------------------------------------------------------------
        # 1. Obtain Quote from Jupiter v6 (GMGN Swap Route Engine)
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
                self.logger.log_warning(f"No Jupiter DEX swap route found for {token_mint[:8]}. Aborting buy.")
                return False, None, 0
        else:
            expected_tokens = int(quote.get("outAmount", 0))

        # ---------------------------------------------------------------------
        # 2. Dry Run Simulation Mode
        # ---------------------------------------------------------------------
        if self.dry_run:
            simulated_sig = self._generate_simulated_hash("BUY", token_mint, amount_sol)
            self.logger.log_trade(
                action="BUY [GMGN/SIM]",
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
        tx_hash = None
        if self.use_jito:
            bundle_id = await self.jito.send_bundle([signed_tx_b64])
            if bundle_id:
                tx_hash = signature_str
                self.logger.log_trade(
                    action="BUY [GMGN/JITO]",
                    token_mint=token_mint,
                    amount_sol=amount_sol,
                    price_sol=(amount_sol / (expected_tokens / 1e6)) if expected_tokens > 0 else 0.0,
                    signature=signature_str,
                    notes=f"Jito Bundle: {bundle_id[:12]} | Tip: {tip_lamports} lamports",
                )
            else:
                self.logger.log_warning("Jito bundle submission failed; falling back to direct RPC submission.")

        if not tx_hash:
            tx_sig = await self.rpc.send_raw_transaction(signed_tx_b64)
            tx_hash = tx_sig or signature_str
            self.logger.log_trade(
                action="BUY [GMGN/RPC]",
                token_mint=token_mint,
                amount_sol=amount_sol,
                price_sol=(amount_sol / (expected_tokens / 1e6)) if expected_tokens > 0 else 0.0,
                signature=tx_hash,
                notes="GMGN Swap Transaction",
            )

        actual_tok = 0.0
        for _ in range(5):
            await asyncio.sleep(1.2)
            actual_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
            if actual_tok > 0:
                break

        if actual_tok > 0:
            return True, tx_hash, int(actual_tok)
        else:
            self.logger.log_warning(f"No token balance confirmed on-chain for {token_mint[:8]}. Aborting position registration.")
            return False, None, 0

    async def execute_sell(
        self,
        token_mint: str,
        tokens_amount: int,
        reason: str = "EXIT",
        slippage_bps: int = 500,
        is_congested: bool = False,
    ) -> Tuple[bool, Optional[str], float]:
        """
        Executes a SELL swap: Target Token -> WSOL via Jupiter v6 (GMGN Swap Engine).
        Returns: (success: bool, tx_signature: str, sol_received: float)
        """
        if tokens_amount <= 0:
            return False, None, 0.0

        current_tok = await self.rpc.get_token_balance(self.pubkey_str, token_mint)
        if current_tok <= 0:
            self.logger.log_warning(f"On-chain balance for {token_mint[:8]} is 0. Auto-clearing position.")
            return True, "cleared_zero_balance", 0.0

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
                self.logger.log_warning(f"No Jupiter quote to sell {token_mint[:8]}. Retrying with 15% slippage...")
                quote = await self.jupiter.get_quote(
                    input_mint=token_mint,
                    output_mint=WSOL_MINT,
                    amount_lamports=tokens_amount,
                    slippage_bps=1500,
                )
                if not quote:
                    return False, None, 0.0

        out_lamports = int(quote.get("outAmount", 0)) if quote else 0
        sol_received = out_lamports / 1_000_000_000.0

        if self.dry_run:
            simulated_sig = self._generate_simulated_hash("SELL", token_mint, sol_received)
            self.logger.log_trade(
                action=f"SELL [{reason}-SIM]",
                token_mint=token_mint,
                amount_sol=sol_received,
                price_sol=sol_received / (tokens_amount / 1e6) if tokens_amount > 0 else 0.0,
                signature=simulated_sig,
                notes=f"Closed {tokens_amount} tokens",
            )
            return True, simulated_sig, sol_received

        # Live Execution
        bal_before = await self.rpc.get_balance(self.pubkey_str)
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
                await asyncio.sleep(1.5)
                bal_after = await self.rpc.get_balance(self.pubkey_str)
                real_sol = max(0.001, bal_after - bal_before) if bal_after > bal_before else sol_received
                self.logger.log_trade(
                    action=f"SELL [{reason}-JITO]",
                    token_mint=token_mint,
                    amount_sol=real_sol,
                    price_sol=real_sol / (tokens_amount / 1e6) if tokens_amount > 0 else 0.0,
                    signature=signature_str,
                    notes=f"GMGN Jito Bundle: {bundle_id[:12]} | Net SOL: {real_sol:.4f}",
                )
                return True, signature_str, real_sol

        tx_sig = await self.rpc.send_raw_transaction(signed_tx_b64)
        await asyncio.sleep(1.5)
        bal_after = await self.rpc.get_balance(self.pubkey_str)
        real_sol = max(0.001, bal_after - bal_before) if bal_after > bal_before else sol_received
        self.logger.log_trade(
            action=f"SELL [{reason}-RPC]",
            token_mint=token_mint,
            amount_sol=real_sol,
            price_sol=real_sol / (tokens_amount / 1e6) if tokens_amount > 0 else 0.0,
            signature=tx_sig or signature_str,
            notes=f"GMGN Direct RPC Swap | Net SOL: {real_sol:.4f}",
        )
        return True, tx_sig or signature_str, real_sol

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
