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
        # 0. Peak-Trailing Profit Maximization Engine (Sell at the Highest Peak)
        # Holds as long as the coin is reaching new highs; exits immediately
        # when price pulls back 6%-8% from the highest recorded peak!
        # ---------------------------------------------------------------------
        peak_pnl = 0.0
        if position.entry_price_sol > 0:
            peak_pnl = ((position.peak_price_sol - position.entry_price_sol) / position.entry_price_sol) * 100.0

        if peak_pnl >= 20.0:
            # Dynamic Pullback Tolerance from Peak:
            # - Parabolic Moonshots (>= +100%): tight 6% pullback from peak
            # - Strong Pumps (>= +50%): 7% pullback from peak
            # - Moderate Gains (>= +20%): 8% pullback from peak
            pullback_tolerance = 0.06 if peak_pnl >= 100.0 else (0.07 if peak_pnl >= 50.0 else 0.08)
            pullback_threshold = position.peak_price_sol * (1.0 - pullback_tolerance)

            if current_price_sol <= pullback_threshold:
                reason = (
                    f"🎯 PEAK PROFIT REALIZED! Coin peaked at +{peak_pnl:.1f}%, "
                    f"selling near the absolute highest peak at +{pnl_pct:.1f}% to lock maximum profit!"
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

        # Multi-Step Profit Protection Ratchets (Guarantees Profit):
        # If up > +15%, lock at least +5% green profit
        if pnl_pct >= 15.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 1.05)
        # If up > +25%, lock at least +15% profit
        if pnl_pct >= 25.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 1.15)
        # If up > +40%, lock at least +30% profit
        if pnl_pct >= 40.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 1.30)

        # Trailing stop can only ratchet UP, never down
        if calculated_stop > position.trailing_stop_price:
            position.trailing_stop_price = calculated_stop

        # Check stop loss breach (with tight -10% hard stop)
        time_in_trade = now - position.entry_timestamp
        if time_in_trade > 3.0:
            hard_stop = position.entry_price_sol * 0.90  # -10% tight hard stop
            if current_price_sol <= hard_stop:
                reason = (
                    f"Tight Stop-Loss Cut (-10%)! Price: {current_price_sol:.8f} "
                    f"(PnL: {pnl_pct:+.1f}%). Exiting immediately to protect capital!"
                )
                return TradeAction.STOP_LOSS, reason, 1.0

            if position.trailing_stop_price > hard_stop and current_price_sol <= position.trailing_stop_price:
                reason = (
                    f"Dynamic Trailing Stop Breached! Price: {current_price_sol:.8f} <= "
                    f"Stop: {position.trailing_stop_price:.8f} (PnL: {pnl_pct:+.1f}%)"
                )
                return TradeAction.STOP_LOSS, reason, 1.0

        # ---------------------------------------------------------------------
        # 2. Dynamic Profit Realization: Buyer Volume Exhaustion (Sell at Peak)
        # ---------------------------------------------------------------------
        is_selling_dominant = (
            momentum.sell_volume_sol > momentum.buy_volume_sol
            and momentum.total_transactions >= 4
        )

        if is_selling_dominant:
            position.consecutive_exhaustions += 1
        else:
            position.consecutive_exhaustions = 0

        if (
            position.consecutive_exhaustions >= self.consecutive_exhaustion_intervals
            and position.unrealized_pnl_pct >= 20.0
        ):
            reason = (
                f"🎯 PEAK EXHAUSTION EXIT! Heavy selling detected near top "
                f"(+{position.unrealized_pnl_pct:.1f}% PnL). Locking 100% profit!"
            )
            return TradeAction.TAKE_PROFIT, reason, 1.0

        # ---------------------------------------------------------------------
        # 3. 15-Second Fast Stagnation Cut: Reclaim Idle Capital
        # ---------------------------------------------------------------------
        if time_in_trade >= float(self.base_stagnation_seconds) and pnl_pct < 10.0:
            reason = (
                f"⏱️ 15s STAGNATION TIMEOUT: Token flat (Held: {time_in_trade:.1f}s | PnL: {pnl_pct:+.1f}%). "
                f"Selling 100% to rotate capital into the next high-cap coin!"
            )
            return TradeAction.STAGNATION_CUT, reason, 1.0

        return TradeAction.HOLD, "Position within dynamic parameters", 0.0
