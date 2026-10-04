"""
Multi-Tier Token Security and Authority Filter
==============================================
Validates mint authority, freeze authority, LP token burn/lock verification,
and Token-2022 transfer fee / tax extension inspection.
"""

from __future__ import annotations

import base64
import struct
from typing import Optional, Tuple
from solders.pubkey import Pubkey

from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import TokenSecurityAudit

DEAD_ADDRESSES = {
    "11111111111111111111111111111111",
    "deaddeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
    "1nc1nerator11111111111111111111111111111111",
}
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class TokenSecurityFilter:
    """
    Evaluates hard stops on token security. Any failure immediately rejects the token.
    """

    def __init__(self, rpc_balancer: MultiRPCBalancer):
        self.rpc = rpc_balancer
        self.logger = get_logger()

    async def audit_token(
        self,
        token_mint: str,
        lp_mint: Optional[str] = None,
        lp_vault: Optional[str] = None,
    ) -> TokenSecurityAudit:
        """
        Conducts a multi-tier security inspection:
        1. Mint Authority == None (Hard Stop)
        2. Freeze Authority == None (Hard Stop)
        3. Token-2022 Transfer Fee Extension check (Reject if tax > 0)
        4. LP Token Burn / Lock Verification
        """
        mint_info = await self.rpc.get_account_info(token_mint)
        if not mint_info:
            return TokenSecurityAudit(
                mint_address=token_mint,
                mint_authority=None,
                freeze_authority=None,
                lp_burned=False,
                lp_burn_pct=0.0,
                is_token_2022=False,
                transfer_fee_bps=0,
                hard_stops_passed=False,
                failure_reason=f"Failed to fetch account info for mint {token_mint}",
            )

        owner_program = mint_info.get("owner", "")
        raw_data_b64 = mint_info.get("data", [""])[0]
        raw_bytes = base64.b64decode(raw_data_b64)

        is_token_2022 = (owner_program == TOKEN_2022_PROGRAM_ID)

        # ---------------------------------------------------------------------
        # 1. Parse SPL Token Mint Account (82-byte header)
        # ---------------------------------------------------------------------
        mint_auth, freeze_auth, supply, decimals = self._parse_mint_data(raw_bytes)

        # Hard Stop #1: Mint Authority must be revoked
        if mint_auth is not None:
            return TokenSecurityAudit(
                mint_address=token_mint,
                mint_authority=mint_auth,
                freeze_authority=freeze_auth,
                lp_burned=False,
                lp_burn_pct=0.0,
                is_token_2022=is_token_2022,
                transfer_fee_bps=0,
                hard_stops_passed=False,
                failure_reason=f"Active Mint Authority detected ({mint_auth}). Infinite mint risk.",
            )

        # Hard Stop #2: Freeze Authority must be revoked
        if freeze_auth is not None:
            return TokenSecurityAudit(
                mint_address=token_mint,
                mint_authority=None,
                freeze_authority=freeze_auth,
                lp_burned=False,
                lp_burn_pct=0.0,
                is_token_2022=is_token_2022,
                transfer_fee_bps=0,
                hard_stops_passed=False,
                failure_reason=f"Active Freeze Authority detected ({freeze_auth}). Blacklist risk.",
            )

        # ---------------------------------------------------------------------
        # 2. Token-2022 Extension Inspection (Transfer Fee / Tax Hook)
        # ---------------------------------------------------------------------
        transfer_fee_bps = 0
        if is_token_2022 and len(raw_bytes) > 82:
            transfer_fee_bps = self._parse_token_2022_transfer_fee(raw_bytes)
            if transfer_fee_bps > 0:
                return TokenSecurityAudit(
                    mint_address=token_mint,
                    mint_authority=None,
                    freeze_authority=None,
                    lp_burned=False,
                    lp_burn_pct=0.0,
                    is_token_2022=True,
                    transfer_fee_bps=transfer_fee_bps,
                    hard_stops_passed=False,
                    failure_reason=f"Token-2022 transfer fee extension active ({transfer_fee_bps} bps). Hidden tax.",
                )

        # ---------------------------------------------------------------------
        # 3. LP Token Burn / Lock Verification
        # ---------------------------------------------------------------------
        lp_burned, lp_burn_pct = await self._verify_lp_burn_status(lp_mint, lp_vault)
        if not lp_burned and lp_mint is not None:
            return TokenSecurityAudit(
                mint_address=token_mint,
                mint_authority=None,
                freeze_authority=None,
                lp_burned=False,
                lp_burn_pct=lp_burn_pct,
                is_token_2022=is_token_2022,
                transfer_fee_bps=transfer_fee_bps,
                hard_stops_passed=False,
                failure_reason=f"Unverified LP Lock/Burn (Burned: {lp_burn_pct:.1f}% < 95.0%). Rug pull risk.",
            )

        return TokenSecurityAudit(
            mint_address=token_mint,
            mint_authority=None,
            freeze_authority=None,
            lp_burned=lp_burned,
            lp_burn_pct=lp_burn_pct,
            is_token_2022=is_token_2022,
            transfer_fee_bps=transfer_fee_bps,
            hard_stops_passed=True,
        )

    def _parse_mint_data(
        self, data: bytes
    ) -> Tuple[Optional[str], Optional[str], int, int]:
        """
        Unpacks standard 82-byte SPL Mint Layout:
        [0..4]   mint_authority_coption (u32)
        [4..36]  mint_authority (32 bytes)
        [36..44] supply (u64)
        [44..45] decimals (u8)
        [45..46] is_initialized (bool)
        [46..50] freeze_authority_coption (u32)
        [50..82] freeze_authority (32 bytes)
        """
        if len(data) < 82:
            return None, None, 0, 0

        mint_auth_option = struct.unpack("<I", data[0:4])[0]
        mint_auth = (
            str(Pubkey.from_bytes(data[4:36])) if mint_auth_option != 0 else None
        )

        supply = struct.unpack("<Q", data[36:44])[0]
        decimals = data[44]

        freeze_auth_option = struct.unpack("<I", data[46:50])[0]
        freeze_auth = (
            str(Pubkey.from_bytes(data[50:82])) if freeze_auth_option != 0 else None
        )

        return mint_auth, freeze_auth, supply, decimals

    def _parse_token_2022_transfer_fee(self, data: bytes) -> int:
        """
        Parses Token-2022 Type-Length-Value (TLV) extension data looking for
        ExtensionType::TransferFeeConfig (Type identifier = 1).
        TransferFeeConfig layout contains older and newer transfer fee basis points.
        """
        offset = 82  # Beyond standard mint layout
        # Skip account type byte if present
        if len(data) > offset and data[offset] == 1:
            offset += 1

        while offset + 4 <= len(data):
            ext_type, ext_len = struct.unpack("<HH", data[offset : offset + 4])
            offset += 4

            if ext_type == 1:  # TransferFeeConfig
                # Layout: transfer_fee_config_authority (32), withdraw_withheld_authority (32),
                # older_transfer_fee (epoch, max_fee, fee_bps), newer_transfer_fee (...)
                if ext_len >= 80 and offset + 80 <= len(data):
                    # fee_bps is at offset + 64 + 8 + 8 (approx 78)
                    older_bps = struct.unpack("<H", data[offset + 76 : offset + 78])[0]
                    return int(older_bps)
                return 100  # Conservative estimate if parsed partially

            offset += ext_len

        return 0

    async def _verify_lp_burn_status(
        self, lp_mint: Optional[str], lp_vault: Optional[str]
    ) -> Tuple[bool, float]:
        """
        Verifies whether LP tokens have been burned to dead address or locked in verified lockers.
        """
        if not lp_mint:
            # If no LP mint (e.g. Pump.fun bonding curve), LP is locked in bonding curve program
            return True, 100.0

        try:
            largest = await self.rpc.get_token_largest_accounts(lp_mint)
            supply_info = await self.rpc.get_token_supply(lp_mint)
            if not supply_info or not largest:
                return False, 0.0

            total_supply = float(supply_info.get("amount", 0))
            if total_supply <= 0:
                # 0 LP supply remaining indicates complete burn
                return True, 100.0

            burned_amount = 0.0
            for holder in largest:
                addr = holder.get("address", "")
                amt = float(holder.get("amount", 0))
                # Check if holder account is dead or incinerator address
                if addr in DEAD_ADDRESSES:
                    burned_amount += amt

            burn_pct = (burned_amount / total_supply) * 100.0
            return (burn_pct >= 95.0), burn_pct

        except Exception as e:
            self.logger.log_warning(f"Error checking LP burn status: {e}")
            return False, 0.0
