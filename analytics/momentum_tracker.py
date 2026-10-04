"""
Volume Inflow Velocity & Retail Momentum Tracker
================================================
Monitors order flow within rolling 3-minute windows, distinguishing
genuine retail momentum from single-wallet wash trading or spoofing,
and evaluating inflow velocity against the 90th percentile baseline.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Set
import numpy as np

from core.logger import get_logger
from core.types import MomentumMetrics


@dataclass
class SwapEvent:
    """Individual trade execution record."""
    signature: str
    user_wallet: str
    is_buy: bool
    sol_amount: float
    token_amount: float
    timestamp: float


class MomentumTracker:
    """
    Maintains rolling sliding windows of transaction history per token,
    evaluating unique buyer arrival rates, buy-volume delta, and spoofing ratios.
    """

    def __init__(
        self,
        window_seconds: int = 180,  # 3 minutes
        min_unique_buyers: int = 12,
        percentile_threshold: float = 90.0,
    ):
        self.window_seconds = window_seconds
        self.min_unique_buyers = min_unique_buyers
        self.percentile_threshold = percentile_threshold
        self.logger = get_logger()

        # token_mint -> deque of SwapEvents
        self._token_events: Dict[str, Deque[SwapEvent]] = {}
        # Historical baseline sample of buyer arrival counts across new pools
        self._historical_buyer_inflows: Deque[int] = deque(maxlen=500)
        # Populate initial empirical baseline distribution
        self._initialize_empirical_baseline()

    def _initialize_empirical_baseline(self) -> None:
        """Seeds baseline distribution representing typical 3-minute new pool launches."""
        # Baseline distribution: median ~8 buyers, 75th percentile ~14, 90th percentile ~22
        np.random.seed(42)
        samples = np.random.lognormal(mean=2.1, sigma=0.6, size=200).astype(int)
        for s in samples:
            self._historical_buyer_inflows.append(int(max(1, s)))

    def record_swap(
        self,
        token_mint: str,
        user_wallet: str,
        is_buy: bool,
        sol_amount: float,
        token_amount: float,
        signature: str,
    ) -> None:
        """Appends swap observation and purges expired records."""
        now = time.time()
        if token_mint not in self._token_events:
            self._token_events[token_mint] = deque()

        event = SwapEvent(
            signature=signature,
            user_wallet=user_wallet,
            is_buy=is_buy,
            sol_amount=sol_amount,
            token_amount=token_amount,
            timestamp=now,
        )
        self._token_events[token_mint].append(event)
        self._prune_window(token_mint, now)

    def _prune_window(self, token_mint: str, current_time: float) -> None:
        """Removes swap events older than window_seconds."""
        events = self._token_events.get(token_mint)
        if not events:
            return

        cutoff = current_time - self.window_seconds
        while events and events[0].timestamp < cutoff:
            events.popleft()

    def evaluate_momentum(self, token_mint: str) -> MomentumMetrics:
        """
        Calculates buyer velocity, spoofing defense ratio, and percentile rank.
        """
        now = time.time()
        self._prune_window(token_mint, now)
        events = self._token_events.get(token_mint, deque())

        if not events:
            return MomentumMetrics(
                token_mint=token_mint,
                window_seconds=self.window_seconds,
                unique_buyers_count=0,
                unique_sellers_count=0,
                total_transactions=0,
                buy_volume_sol=0.0,
                sell_volume_sol=0.0,
                volume_delta_sol=0.0,
                buyer_growth_percentile=0.0,
                is_90th_percentile=False,
            )

        buyer_wallets: Set[str] = set()
        seller_wallets: Set[str] = set()
        buy_volume = 0.0
        sell_volume = 0.0
        total_txs = len(events)

        wallet_buy_counts: Dict[str, int] = {}

        for e in events:
            if e.is_buy:
                buyer_wallets.add(e.user_wallet)
                buy_volume += e.sol_amount
                wallet_buy_counts[e.user_wallet] = wallet_buy_counts.get(e.user_wallet, 0) + 1
            else:
                seller_wallets.add(e.user_wallet)
                sell_volume += e.sol_amount

        unique_buyers = len(buyer_wallets)
        unique_sellers = len(seller_wallets)
        volume_delta = buy_volume - sell_volume

        # ---------------------------------------------------------------------
        # Anti-Spoofing & Wash Trading Filter
        # ---------------------------------------------------------------------
        # If a single wallet accounts for > 40% of all buy transactions,
        # penalize unique buyer count as artificial wash trading.
        if unique_buyers > 0 and wallet_buy_counts:
            max_single_wallet_buys = max(wallet_buy_counts.values())
            total_buys = sum(wallet_buy_counts.values())
            if total_buys >= 5 and (max_single_wallet_buys / total_buys) > 0.40:
                # Deduct spoofed count
                unique_buyers = max(1, unique_buyers - 5)

        # Record into historical distribution
        if unique_buyers > 0:
            self._historical_buyer_inflows.append(unique_buyers)

        # ---------------------------------------------------------------------
        # 90th Percentile Calculation
        # ---------------------------------------------------------------------
        if len(self._historical_buyer_inflows) >= 20:
            baseline = np.array(self._historical_buyer_inflows)
            # Calculate percentile of current unique buyer inflow
            percentile = float(np.mean(baseline <= unique_buyers) * 100.0)
            threshold_value = float(np.percentile(baseline, self.percentile_threshold))
        else:
            percentile = 50.0
            threshold_value = 15.0

        is_90th = (
            unique_buyers >= self.min_unique_buyers
            and percentile >= self.percentile_threshold
            and volume_delta > 0
        )

        return MomentumMetrics(
            token_mint=token_mint,
            window_seconds=self.window_seconds,
            unique_buyers_count=unique_buyers,
            unique_sellers_count=unique_sellers,
            total_transactions=total_txs,
            buy_volume_sol=buy_volume,
            sell_volume_sol=sell_volume,
            volume_delta_sol=volume_delta,
            buyer_growth_percentile=percentile,
            is_90th_percentile=is_90th,
        )
