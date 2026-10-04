"""
Dynamic Liquidity Health & LP-to-FDV Ratio Evaluator
====================================================
Evaluates pool depth against fully diluted valuation (FDV), rejecting
disproportionately thin pools designed to artificially inflate market caps.
"""

from __future__ import annotations

from typing import Optional, Tuple
from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import LiquidityMetrics


class LiquidityHealthFilter:
    """
    Evaluates real on-chain liquidity depth, reserve ratios, and valuation health.
    """

    def __init__(
        self,
        rpc_balancer: MultiRPCBalancer,
        min_sol_reserves: float = 15.0,
        min_lp_to_mc_ratio: float = 0.08,  # 8% minimum liquidity backing
    ):
        self.rpc = rpc_balancer
        self.min_sol_reserves = min_sol_reserves
        self.min_lp_to_mc_ratio = min_lp_to_mc_ratio
        self.logger = get_logger()

    async def evaluate_pool_liquidity(
        self,
        pool_address: str,
        token_mint: str,
        sol_reserves: float,
        token_reserves: float,
    ) -> LiquidityMetrics:
        """
        Calculates price, FDV, and LP-to-FDV ratio.
        """
        # Hard check on minimum absolute SOL reserves
        if sol_reserves < self.min_sol_reserves:
            return LiquidityMetrics(
                pool_address=pool_address,
                sol_reserves=sol_reserves,
                token_reserves=token_reserves,
                price_sol=0.0,
                fdv_sol=0.0,
                lp_to_mc_ratio=0.0,
                is_sufficient=False,
                failure_reason=(
                    f"Insufficient SOL liquidity ({sol_reserves:.2f} SOL < "
                    f"Minimum {self.min_sol_reserves:.2f} SOL)."
                ),
            )

        if token_reserves <= 0:
            return LiquidityMetrics(
                pool_address=pool_address,
                sol_reserves=sol_reserves,
                token_reserves=token_reserves,
                price_sol=0.0,
                fdv_sol=0.0,
                lp_to_mc_ratio=0.0,
                is_sufficient=False,
                failure_reason="Invalid pool: token reserves <= 0",
            )

        # Marginal AMM price in SOL
        price_sol = sol_reserves / token_reserves

        # Query total token supply
        supply_info = await self.rpc.get_token_supply(token_mint)
        if not supply_info:
            return LiquidityMetrics(
                pool_address=pool_address,
                sol_reserves=sol_reserves,
                token_reserves=token_reserves,
                price_sol=price_sol,
                fdv_sol=0.0,
                lp_to_mc_ratio=0.0,
                is_sufficient=False,
                failure_reason="Could not query token supply to compute FDV",
            )

        # Normalize supply to UI token units matching token_reserves
        ui_amt = supply_info.get("uiAmount")
        decimals = int(supply_info.get("decimals", 6))
        raw_amount = float(supply_info.get("amount", 0))
        total_supply = float(ui_amt) if ui_amt is not None and float(ui_amt) > 0 else (raw_amount / (10 ** decimals))
        if total_supply <= 0:
            return LiquidityMetrics(
                pool_address=pool_address,
                sol_reserves=sol_reserves,
                token_reserves=token_reserves,
                price_sol=price_sol,
                fdv_sol=0.0,
                lp_to_mc_ratio=0.0,
                is_sufficient=False,
                failure_reason="Total supply returned is 0",
            )

        # Fully Diluted Valuation (FDV) in SOL
        fdv_sol = total_supply * price_sol

        # Total LP Value in SOL (Constant Product AMM holds 50% SOL, 50% Token)
        total_lp_sol = 2.0 * sol_reserves

        # LP to Market Cap (FDV) Ratio
        # Mathematically equivalent to: 2 * (token_reserves in pool / total_supply)
        lp_to_mc_ratio = total_lp_sol / fdv_sol if fdv_sol > 0 else 0.0

        is_sufficient = True
        failure_reason = None

        if lp_to_mc_ratio < self.min_lp_to_mc_ratio:
            is_sufficient = False
            failure_reason = (
                f"Disproportionately thin liquidity: LP-to-FDV ratio is {lp_to_mc_ratio*100:.2f}% "
                f"(Threshold: >={self.min_lp_to_mc_ratio*100:.1f}%). "
                f"FDV: {fdv_sol:.1f} SOL backed by only {total_lp_sol:.1f} SOL LP."
            )

        return LiquidityMetrics(
            pool_address=pool_address,
            sol_reserves=sol_reserves,
            token_reserves=token_reserves,
            price_sol=price_sol,
            fdv_sol=fdv_sol,
            lp_to_mc_ratio=lp_to_mc_ratio,
            is_sufficient=is_sufficient,
            failure_reason=failure_reason,
        )
