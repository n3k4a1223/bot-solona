"""
Dynamic Exit Engine (Volatility Trailing Stop, Exhaustion Scale-Out, Stagnation Cut)
===================================================================================
Replaces static take-profit and stop-loss targets with continuous volatility bands,
order flow delta exhaustion scale-outs, and volatility-adapted stagnation timeouts.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple
from core.logger import get_logger
from core.types import (
    MomentumMetrics,
    OpenPosition,
    TradeAction,
    VolatilityMetrics,
)


class DynamicExitEngine:
    """
    Evaluates real-time exit conditions for all active positions.
    """

    def __init__(
        self,
        atr_multiplier: float = 2.0,
        consecutive_exhaustion_intervals: int = 2,
        base_stagnation_seconds: int = 120,
        min_volume_multiplier_baseline: float = 2.0,
        target_take_profit_pct: float = 100.0,  # 100% Take-Profit target (2x double capital)
    ):
        self.atr_multiplier = atr_multiplier
        self.consecutive_exhaustion_intervals = consecutive_exhaustion_intervals
        self.base_stagnation_seconds = base_stagnation_seconds
        self.min_volume_multiplier_baseline = min_volume_multiplier_baseline
        self.target_take_profit_pct = target_take_profit_pct
        self.logger = get_logger()

    def evaluate_position_exit(
        self,
        position: OpenPosition,
        current_price_sol: float,
        volatility: VolatilityMetrics,
        momentum: MomentumMetrics,
    ) -> Tuple[TradeAction, str, float]:
        """
        Evaluates position state against dynamic exit algorithms:
        0. Master Take-Profit Target (+100% / 2x Double Capital)
        1. Dynamic ATR Trailing Stop (with +25% and +50% breakeven ratchets)
        2. Order Flow Volume Delta Exhaustion Scale-Out
        3. Adaptive Stagnation Capital Reclaim
        Returns: (TradeAction, reason_str, fraction_to_sell)
        """
        now = time.time()
        position.current_price_sol = current_price_sol

        # Update high-water mark peak price
        if current_price_sol > position.peak_price_sol:
            position.peak_price_sol = current_price_sol

        pnl_pct = position.unrealized_pnl_pct

        # ---------------------------------------------------------------------
        # 0. Master Take-Profit Target: +100% Gain (2x Capital Doubler)
        # Sells 100% immediately to rotate compounded capital into next token!
        # ---------------------------------------------------------------------
        if pnl_pct >= self.target_take_profit_pct or current_price_sol >= (position.entry_price_sol * (1.0 + self.target_take_profit_pct / 100.0)):
            reason = (
                f"🎯 TARGET DOUBLED (+{pnl_pct:.1f}% >= +{self.target_take_profit_pct:.1f}%)! "
                f"Capital doubled from {position.sol_invested:.4f} SOL. "
                f"Selling 100% of tokens to rotate compounded SOL into the next token!"
            )
            return TradeAction.TAKE_PROFIT, reason, 1.0

        # ---------------------------------------------------------------------
        # 1. Update Dynamic ATR / Volatility Trailing Stop Loss Band
        # ---------------------------------------------------------------------
        vol_distance = max(
            volatility.atr_sol,
            position.peak_price_sol * max(volatility.std_1m_returns, 0.02),
        )
        calculated_stop = position.peak_price_sol - (self.atr_multiplier * vol_distance)

        # Profit Protection Ratchets:
        # If up > +25%, guarantee breakeven (stop cannot fall below entry price)
        if pnl_pct >= 25.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol)
        # If up > +50%, guarantee at least +25% profit lock
        if pnl_pct >= 50.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 1.25)

        # Trailing stop can only ratchet UP, never down
        if calculated_stop > position.trailing_stop_price:
            position.trailing_stop_price = calculated_stop

        # Check trailing stop breach
        if current_price_sol <= position.trailing_stop_price:
            pnl_pct = position.unrealized_pnl_pct
            reason = (
                f"Dynamic Trailing Stop Breached! Price: {current_price_sol:.8f} <= "
                f"Stop: {position.trailing_stop_price:.8f} (PnL: {pnl_pct:+.2f}%)"
            )
            return TradeAction.STOP_LOSS, reason, 1.0

        # ---------------------------------------------------------------------
        # 2. Dynamic Profit Realization: Buyer Volume Exhaustion Scale-Out
        # Scale out when Buy Volume < Sell Volume across consecutive intervals
        # ---------------------------------------------------------------------
        # Evaluate order flow delta
        is_selling_dominant = (
            momentum.sell_volume_sol > momentum.buy_volume_sol
            and momentum.total_transactions >= 4
        )

        if is_selling_dominant:
            position.consecutive_exhaustions += 1
        else:
            position.consecutive_exhaustions = 0

        # Trigger scale-out if exhaustion persists and position is in profit (> 6%)
        if (
            position.consecutive_exhaustions >= self.consecutive_exhaustion_intervals
            and position.unrealized_pnl_pct >= 6.0
        ):
            if position.scaled_out_count == 0:
                # Step 1: Scale out 50% to lock gains, tighten stop to entry (breakeven protection)
                position.scaled_out_count = 1
                position.consecutive_exhaustions = 0
                position.trailing_stop_price = max(position.trailing_stop_price, position.entry_price_sol)
                reason = (
                    f"Buyer Exhaustion Scale-Out (50%): Sell delta > Buy delta over 2 intervals. "
                    f"Locking +{position.unrealized_pnl_pct:.1f}% gain and ratcheting stop to breakeven."
                )
                return TradeAction.SCALE_OUT, reason, 0.50

            elif position.scaled_out_count == 1:
                # Step 2: Final scale-out of remaining 50% on renewed exhaustion
                reason = (
                    f"Secondary Buyer Exhaustion (Final 50%): Momentum exhausted at "
                    f"+{position.unrealized_pnl_pct:.1f}% PnL. Fully exiting position."
                )
                return TradeAction.SCALE_OUT, reason, 1.0

        # ---------------------------------------------------------------------
        # 3. Adaptive Stagnation Cut: Reclaim Idle Capital
        # Dynamic timeout scaled inversely with market volatility regime
        # ---------------------------------------------------------------------
        time_in_trade = now - position.entry_timestamp

        if volatility.regime == "EXTREME":
            dynamic_timeout = self.base_stagnation_seconds * 0.5   # 60s
        elif volatility.regime == "HIGH":
            dynamic_timeout = self.base_stagnation_seconds * 0.75  # 90s
        elif volatility.regime == "LOW":
            dynamic_timeout = self.base_stagnation_seconds * 1.5   # 180s
        else:
            dynamic_timeout = self.base_stagnation_seconds         # 120s

        # If holding duration exceeded timeout and volume velocity has collapsed
        is_volume_stagnant = (
            momentum.total_transactions < 3
            or (momentum.buy_volume_sol + momentum.sell_volume_sol) < 0.5
        )
        is_price_flat = abs(position.unrealized_pnl_pct) < 4.0

        if time_in_trade > dynamic_timeout and is_volume_stagnant and is_price_flat:
            reason = (
                f"Adaptive Stagnation Cut: Position inactive for {time_in_trade:.0f}s "
                f"(Timeout: {dynamic_timeout:.0f}s, Regime: {volatility.regime}). "
                f"Reclaiming idle capital for high-velocity setups."
            )
            return TradeAction.STAGNATION_CUT, reason, 1.0

        return TradeAction.HOLD, "Position within dynamic parameters", 0.0
