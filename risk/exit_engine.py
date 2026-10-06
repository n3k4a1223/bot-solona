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
        # 0. Master Take-Profit Target: Fast Scalp (+50% Quick Profit)
        # Sells 100% immediately to lock pure profit!
        # ---------------------------------------------------------------------
        if pnl_pct >= self.target_take_profit_pct or current_price_sol >= (position.entry_price_sol * (1.0 + self.target_take_profit_pct / 100.0)):
            reason = (
                f"🎯 FAST SCALP PROFIT REALIZED (+{pnl_pct:.1f}% >= +{self.target_take_profit_pct:.1f}%)! "
                f"Selling 100% of tokens to lock pure profit and rotate SOL into the next coin!"
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
        # 2. Dynamic Profit Realization: Buyer Volume Exhaustion Scale-Out
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
            and position.unrealized_pnl_pct >= 25.0
        ):
            if position.scaled_out_count == 0:
                position.scaled_out_count = 1
                position.consecutive_exhaustions = 0
                reason = (
                    f"Buyer Exhaustion (50% scale-out): Momentum slowing at "
                    f"+{position.unrealized_pnl_pct:.1f}% PnL. Locking profit!"
                )
                return TradeAction.SCALE_OUT, reason, 0.50

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
