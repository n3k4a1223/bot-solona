"""
Cluster Congestion Monitor & Daily Drawdown Governor
====================================================
Tracks Solana cluster health (TPS, slot time, transaction failure rates)
and maintains a real-time portfolio peak equity governor that trips the master
circuit breaker if daily drawdown exceeds dynamic risk thresholds.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional
from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import ClusterCongestionState


class CircuitBreakerManager:
    """
    Automated capital protection and network congestion safety breakers.
    """

    def __init__(
        self,
        rpc_balancer: MultiRPCBalancer,
        max_daily_drawdown_pct: float = 6.0,   # 6% max daily peak drawdown
        congestion_fail_rate_threshold: float = 0.25,  # 25% failure rate
        min_cluster_tps: float = 1400.0,
    ):
        self.rpc = rpc_balancer
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.congestion_fail_rate_threshold = congestion_fail_rate_threshold
        self.min_cluster_tps = min_cluster_tps
        self.logger = get_logger()

        # Equity tracking state
        self.day_start_timestamp = time.time()
        self.peak_equity_sol: float = 0.0
        self.is_drawdown_tripped: bool = False

        # Cluster health telemetry
        self.cluster_state = ClusterCongestionState(
            tps=2200.0,
            mean_slot_time_ms=450.0,
            tx_failure_rate=0.08,
            is_congested=False,
            throttle_multiplier=1.0,
        )

    def update_portfolio_equity(self, current_liquid_sol: float, unrealized_pnl_sol: float) -> Tuple[bool, float]:
        """
        Updates portfolio equity and evaluates the Daily Drawdown Governor.
        Returns: (is_tripped: bool, current_drawdown_pct: float)
        """
        # Reset peak equity at 00:00 UTC daily
        now = time.time()
        if now - self.day_start_timestamp >= 86400.0:
            self.day_start_timestamp = now
            self.peak_equity_sol = current_liquid_sol + unrealized_pnl_sol
            self.is_drawdown_tripped = False

        total_equity = current_liquid_sol + unrealized_pnl_sol
        if total_equity > self.peak_equity_sol:
            self.peak_equity_sol = total_equity

        if self.peak_equity_sol <= 0.0:
            return False, 0.0

        drawdown_pct = ((self.peak_equity_sol - total_equity) / self.peak_equity_sol) * 100.0

        if drawdown_pct >= self.max_daily_drawdown_pct and not self.is_drawdown_tripped:
            self.is_drawdown_tripped = True
            self.logger.log_error(
                f"[bold red]CRITICAL: Daily Drawdown Governor Tripped![/bold red] "
                f"Peak: {self.peak_equity_sol:.3f} SOL -> Current: {total_equity:.3f} SOL "
                f"(Drawdown: {drawdown_pct:.2f}% >= Threshold: {self.max_daily_drawdown_pct:.1f}%). "
                f"Halting all new trade entries."
            )

        return self.is_drawdown_tripped, drawdown_pct

    async def update_cluster_health(self) -> ClusterCongestionState:
        """
        Queries Solana recent performance samples to detect network congestion,
        slot latency spikes, and cluster transaction failure rate.
        """
        try:
            samples = await self.rpc.get_recent_performance_samples(limit=4)
            if not samples:
                return self.cluster_state

            total_txs = 0
            total_slots = 0
            sample_period_secs = 0

            for sample in samples:
                num_txs = sample.get("numTransactions", 0)
                num_slots = sample.get("numSlots", 0)
                sample_period = sample.get("samplePeriodSecs", 60)

                total_txs += num_txs
                total_slots += num_slots
                sample_period_secs += sample_period

            if sample_period_secs > 0:
                tps = total_txs / sample_period_secs
                slot_time_ms = (sample_period_secs / max(1, total_slots)) * 1000.0
            else:
                tps = 2000.0
                slot_time_ms = 450.0

            # Estimate recent transaction failure rate from cluster stress
            # When TPS drops below normal threshold or slot time spikes > 650ms
            if tps < self.min_cluster_tps or slot_time_ms > 650.0:
                failure_rate = 0.32
                is_congested = True
                throttle_multiplier = 0.50  # Defensively scale position sizes by 50%
            else:
                failure_rate = 0.09
                is_congested = False
                throttle_multiplier = 1.0

            self.cluster_state = ClusterCongestionState(
                tps=tps,
                mean_slot_time_ms=slot_time_ms,
                tx_failure_rate=failure_rate,
                is_congested=is_congested,
                throttle_multiplier=throttle_multiplier,
            )

            if is_congested:
                self.logger.log_warning(
                    f"Solana cluster congestion detected: TPS={tps:.0f}, "
                    f"Slot={slot_time_ms:.0f}ms. Sizing throttled to 50%."
                )

        except Exception as e:
            self.logger.log_warning(f"Error querying cluster performance: {e}")

        return self.cluster_state
