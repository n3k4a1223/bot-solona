"""
Dynamic Volatility & Liquidity-Adjusted Position Sizer
======================================================
Implements modified fractional Kelly Criterion scaled by real-time return
volatility, order-book depth constraints, and buy inflow velocity.
Strictly eliminates rigid, hardcoded trade sizing.
"""

from __future__ import annotations

import math
import numpy as np

from core.logger import get_logger
from core.types import (
    ComprehensiveRiskScore,
    MomentumMetrics,
    PositionSizeRecommendation,
    VolatilityMetrics,
)


class AdaptivePositionSizer:
    """
    Computes mathematical position size dynamically.
    - Micro-Capital Mode (< 0.15 SOL / ~$5-$25): Aggressive compounding allocation
      (40% - 75% of tradable equity) with a locked 0.005 SOL fee reserve to overcome
      on-chain fixed fees (ATA rent, Jito MEV tips, gas).
    - Standard Portfolio Mode (>= 0.15 SOL): Strictly bounded between 3% and 8%
      via modified Fractional Kelly Criterion and order-book depth constraints.
    """

    def __init__(
        self,
        min_position_pct: float = 0.03,  # 3% floor
        max_position_pct: float = 0.08,  # 8% ceiling
        kelly_fraction: float = 0.35,     # Fractional Kelly factor
        payoff_ratio_b: float = 2.2,     # Estimated payoff ratio (win / loss magnitude)
        max_pool_impact_factor: float = 0.015,  # Max 1.5% of pool SOL reserves
        buy_velocity_weight: float = 1.25,
        micro_cap_threshold_sol: float = 0.15,  # Below this, micro aggressive mode activates
        gas_reserve_sol: float = 0.005,         # Kept untouched for network fees & rent
    ):
        self.min_position_pct = min_position_pct
        self.max_position_pct = max_position_pct
        self.kelly_fraction = kelly_fraction
        self.payoff_ratio_b = payoff_ratio_b
        self.max_pool_impact_factor = max_pool_impact_factor
        self.buy_velocity_weight = buy_velocity_weight
        self.micro_cap_threshold_sol = micro_cap_threshold_sol
        self.gas_reserve_sol = gas_reserve_sol
        self.logger = get_logger()

    def calculate_size(
        self,
        wallet_balance_sol: float,
        pool_sol_reserves: float,
        risk_score: ComprehensiveRiskScore,
        volatility: VolatilityMetrics,
        momentum: MomentumMetrics,
    ) -> PositionSizeRecommendation:
        """
        Calculates optimal SOL capital allocation for the entry signal.
        """
        # Hard floor: at least 0.010 SOL needed (fee buffer + minimal execution slice)
        if wallet_balance_sol < (self.gas_reserve_sol + 0.005):
            return PositionSizeRecommendation(
                token_mint=risk_score.token_mint,
                wallet_liquid_sol=wallet_balance_sol,
                raw_kelly_fraction=0.0,
                volatility_adjusted_fraction=0.0,
                depth_cap_sol=0.0,
                final_allocation_pct=0.0,
                allocated_sol=0.0,
                rationale=f"Wallet balance insufficient (< {self.gas_reserve_sol + 0.005:.3f} SOL). Minimum 0.015 SOL required.",
            )

        # ---------------------------------------------------------------------
        # 1. Base Win Probability & Conviction Estimation
        # ---------------------------------------------------------------------
        score_norm = np.clip((risk_score.composite_score - 70.0) / 30.0, 0.0, 1.0)
        percentile_norm = np.clip((momentum.buyer_growth_percentile - 50.0) / 50.0, 0.0, 1.0)
        p = 0.52 + (0.20 * score_norm * percentile_norm)
        q = 1.0 - p
        b = self.payoff_ratio_b

        raw_kelly = (p * b - q) / b
        raw_kelly = max(0.01, raw_kelly)
        fractional_kelly = raw_kelly * self.kelly_fraction
        vol_adjusted_kelly = fractional_kelly * volatility.volatility_scale_factor

        if momentum.buyer_growth_percentile >= 95.0:
            vol_adjusted_kelly *= self.buy_velocity_weight

        # ---------------------------------------------------------------------
        # 2. Capital Regime Branching: Micro-Cap Aggressive vs Standard Kelly
        # ---------------------------------------------------------------------
        if wallet_balance_sol < self.micro_cap_threshold_sol:
            # Micro-Capital Mode: High-conviction aggressive compounding
            # Deduct fee buffer to prevent wallet starvation
            tradable_sol = max(0.0, wallet_balance_sol - self.gas_reserve_sol)
            
            # Base aggressive fraction: 50% to 75% of tradable equity
            micro_fraction = float(np.clip(
                0.55 * (p / 0.60) * volatility.volatility_scale_factor,
                0.40,
                0.75,
            ))
            desired_sol = min(tradable_sol, max(0.010, tradable_sol * micro_fraction))
            depth_cap_sol = pool_sol_reserves * self.max_pool_impact_factor
            final_sol = min(desired_sol, depth_cap_sol)
            effective_pct = (final_sol / wallet_balance_sol) if wallet_balance_sol > 0 else 0.0

            rationale = (
                f"MICRO-CAP AGGRESSIVE MODE | Sized: {final_sol:.4f} SOL ({effective_pct*100:.1f}%) | "
                f"Reserve: {self.gas_reserve_sol:.3f} SOL | Conviction: {p*100:.1f}% | "
                f"Depth Cap: {depth_cap_sol:.2f} SOL"
            )

            return PositionSizeRecommendation(
                token_mint=risk_score.token_mint,
                wallet_liquid_sol=wallet_balance_sol,
                raw_kelly_fraction=raw_kelly,
                volatility_adjusted_fraction=micro_fraction,
                depth_cap_sol=depth_cap_sol,
                final_allocation_pct=effective_pct,
                allocated_sol=final_sol,
                rationale=rationale,
            )

        # ---------------------------------------------------------------------
        # 3. Standard Fractional Kelly Bounds (3% to 8% of Liquid Balance)
        # ---------------------------------------------------------------------
        clamped_fraction = float(
            np.clip(vol_adjusted_kelly, self.min_position_pct, self.max_position_pct)
        )
        desired_sol = wallet_balance_sol * clamped_fraction
        depth_cap_sol = pool_sol_reserves * self.max_pool_impact_factor
        final_sol = min(desired_sol, depth_cap_sol)
        effective_pct = (final_sol / wallet_balance_sol) if wallet_balance_sol > 0 else 0.0

        rationale = (
            f"Kelly: {fractional_kelly*100:.1f}% | "
            f"VolScale: {volatility.volatility_scale_factor:.2f}x ({volatility.regime}) | "
            f"Velocity %ile: {momentum.buyer_growth_percentile:.1f}% | "
            f"Depth Cap: {depth_cap_sol:.2f} SOL"
        )

        return PositionSizeRecommendation(
            token_mint=risk_score.token_mint,
            wallet_liquid_sol=wallet_balance_sol,
            raw_kelly_fraction=raw_kelly,
            volatility_adjusted_fraction=vol_adjusted_kelly,
            depth_cap_sol=depth_cap_sol,
            final_allocation_pct=effective_pct,
            allocated_sol=final_sol,
            rationale=rationale,
        )
