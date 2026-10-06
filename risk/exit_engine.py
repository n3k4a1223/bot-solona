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
        # 0. Master Take-Profit Target: 10x Moonshot (+900% / +1000% Gain)
        # Sells 100% immediately to lock 10x profits!
        # ---------------------------------------------------------------------
        if pnl_pct >= self.target_take_profit_pct or current_price_sol >= (position.entry_price_sol * (1.0 + self.target_take_profit_pct / 100.0)):
            reason = (
                f"🚀 10x MOONSHOT TARGET REACHED (+{pnl_pct:.1f}% >= +{self.target_take_profit_pct:.1f}%)! "
                f"Capital multiplied 10x from {position.sol_invested:.4f} SOL. "
                f"Selling 100% of tokens to lock 10x profits!"
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

        # Multi-Step Profit Protection Ratchets for 10x Moonshot:
        # If up > +50%, guarantee breakeven (stop cannot fall below entry price)
        if pnl_pct >= 50.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol)
        # If up > +100% (2x), lock at least +50% profit
        if pnl_pct >= 100.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 1.50)
        # If up > +300% (4x), lock at least +200% profit
        if pnl_pct >= 300.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 3.00)
        # If up > +500% (6x), lock at least +400% profit
        if pnl_pct >= 500.0:
            calculated_stop = max(calculated_stop, position.entry_price_sol * 5.00)

        # Trailing stop can only ratchet UP, never down
        if calculated_stop > position.trailing_stop_price:
            position.trailing_stop_price = calculated_stop

        # Check stop loss breach (with 5s grace period for newly opened positions)
        time_in_trade = now - position.entry_timestamp
        if time_in_trade > 5.0:
            hard_stop = position.entry_price_sol * 0.65  # -35% hard stop
            if current_price_sol <= hard_stop:
                reason = (
                    f"Stop-Loss Cut (-35%)! Price dropped to {current_price_sol:.8f} "
                    f"(PnL: {pnl_pct:+.1f}%). Exiting to protect remaining capital."
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

        # Trigger scale-out only after big run-up (> 200%) if exhaustion persists
        if (
            position.consecutive_exhaustions >= self.consecutive_exhaustion_intervals
            and position.unrealized_pnl_pct >= 200.0
        ):
            if position.scaled_out_count == 0:
                position.scaled_out_count = 1
                position.consecutive_exhaustions = 0
                position.trailing_stop_price = max(position.trailing_stop_price, position.entry_price_sol * 2.0)
                reason = (
                    f"Buyer Exhaustion Scale-Out (50%): Sell delta > Buy delta. "
                    f"Locking +{position.unrealized_pnl_pct:.1f}% gain and ratcheting stop to 2x."
                )
                return TradeAction.SCALE_OUT, reason, 0.50

        # ---------------------------------------------------------------------
        # 3. Stagnation Timeout Cut: Reclaim Idle Capital
        # ---------------------------------------------------------------------
        if time_in_trade >= float(self.base_stagnation_seconds) and pnl_pct < 20.0:
            reason = (
                f"⏱️ STAGNATION TIMEOUT CUT: No momentum reached within {self.base_stagnation_seconds}s "
                f"(Held: {time_in_trade:.1f}s | PnL: {pnl_pct:+.1f}%). "
                f"Directly selling 100% to reclaim capital for the next coin!"
            )
            return TradeAction.STAGNATION_CUT, reason, 1.0

        return TradeAction.HOLD, "Position within dynamic parameters", 0.0
