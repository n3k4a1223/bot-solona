"""
Holder Distribution Entropy & Concentration Analyzer
=====================================================
Analyzes non-pool token holders using Shannon Entropy, Gini inequality,
and concentration thresholds (<20% aggregate for top 10, <4% max single wallet).
"""

from __future__ import annotations

import math
from typing import List, Set
import numpy as np

from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import HolderDistribution

# Known DEX / AMM Pool Vaults and Program Wallets to filter out of holder metrics
SYSTEM_AND_AMM_ACCOUNTS: Set[str] = {
    "11111111111111111111111111111111",
    "deaddeaddeaddeaddeaddeaddeaddeaddeaddeaddead",
    "1nc1nerator11111111111111111111111111111111",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium V4
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium Authority
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # Pump.fun
    "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",  # Pump.fun Fee
}


class HolderDistributionAnalyzer:
    """
    Computes statistical dispersion and decentralization entropy across token holders.
    Detects insider cabal wallet clustering and disproportionate supply concentration.
    """

    def __init__(
        self,
        rpc_balancer: MultiRPCBalancer,
        max_top10_percent: float = 20.0,
        max_single_percent: float = 4.0,
        min_shannon_entropy: float = 2.2,
    ):
        self.rpc = rpc_balancer
        self.max_top10_percent = max_top10_percent
        self.max_single_percent = max_single_percent
        self.min_shannon_entropy = min_shannon_entropy
        self.logger = get_logger()

    async def analyze(
        self,
        token_mint: str,
        pool_vaults: Optional[List[str]] = None,
    ) -> HolderDistribution:
        """
        Fetches largest token accounts and calculates concentration & entropy.
        """
        known_vaults = set(SYSTEM_AND_AMM_ACCOUNTS)
        if pool_vaults:
            known_vaults.update(pool_vaults)

        supply_info = await self.rpc.get_token_supply(token_mint)
        if not supply_info:
            return HolderDistribution(
                mint_address=token_mint,
                top10_aggregate_pct=100.0,
                max_single_pct=100.0,
                shannon_entropy=0.0,
                gini_coefficient=1.0,
                total_holders_sampled=0,
                is_healthy=False,
                failure_reason="Unable to query token supply from RPC",
            )

        total_supply = float(supply_info.get("amount", 0))
        if total_supply <= 0:
            return HolderDistribution(
                mint_address=token_mint,
                top10_aggregate_pct=100.0,
                max_single_pct=100.0,
                shannon_entropy=0.0,
                gini_coefficient=1.0,
                total_holders_sampled=0,
                is_healthy=False,
                failure_reason="Total supply is 0",
            )

        largest_accounts = await self.rpc.get_token_largest_accounts(token_mint)
        if not largest_accounts:
            return HolderDistribution(
                mint_address=token_mint,
                top10_aggregate_pct=100.0,
                max_single_pct=100.0,
                shannon_entropy=0.0,
                gini_coefficient=1.0,
                total_holders_sampled=0,
                is_healthy=False,
                failure_reason="No holder accounts returned by RPC",
            )

        # Filter out DEX liquidity vaults, bonding curve accounts, and burn accounts
        # Note: In bonding curve pools (e.g. pump.fun), the pool bonding curve token account
        # holds 50-80% of total supply. Any account holding >= 20% of supply is the pool reserve.
        non_pool_holders = []
        for acc in largest_accounts:
            address = acc.get("address", "")
            amount = float(acc.get("amount", 0))
            if address in known_vaults:
                continue
            if total_supply > 0 and (amount / total_supply) >= 0.20:
                continue
            non_pool_holders.append((address, amount))

        if not non_pool_holders:
            return HolderDistribution(
                mint_address=token_mint,
                top10_aggregate_pct=0.0,
                max_single_pct=0.0,
                shannon_entropy=3.0,
                gini_coefficient=0.0,
                total_holders_sampled=0,
                is_healthy=True,
                failure_reason=None,
            )

        # Sort descending by holdings
        non_pool_holders.sort(key=lambda x: x[1], reverse=True)

        # Take top 10 non-pool holders
        top10 = non_pool_holders[:10]
        top10_sum = sum(amt for _, amt in top10)
        top10_pct = (top10_sum / total_supply) * 100.0

        # Maximum single non-pool wallet holding
        max_single_pct = (top10[0][1] / total_supply) * 100.0 if top10 else 0.0

        # Compute Shannon Entropy H = - sum(p_i * ln(p_i)) over normalized holder distribution
        holder_amounts = np.array([amt for _, amt in non_pool_holders], dtype=np.float64)
        total_sample_amount = np.sum(holder_amounts)

        if total_sample_amount > 0:
            probs = holder_amounts / total_sample_amount
            # Remove zero probabilities to prevent log(0)
            probs = probs[probs > 0]
            shannon_entropy = float(-np.sum(probs * np.log(probs)))
            gini = self._calculate_gini(holder_amounts)
        else:
            shannon_entropy = 0.0
            gini = 1.0

        # Evaluate risk conditions
        is_healthy = True
        failure_reason = None

        if top10_pct > self.max_top10_percent:
            is_healthy = False
            failure_reason = (
                f"Top 10 non-pool holders hold {top10_pct:.1f}% of supply "
                f"(Threshold: <{self.max_top10_percent:.1f}%). High cabal concentration."
            )
        elif max_single_pct > self.max_single_percent:
            is_healthy = False
            failure_reason = (
                f"Single private wallet holds {max_single_pct:.1f}% of supply "
                f"(Threshold: <{self.max_single_percent:.1f}%). High dump risk."
            )
        elif shannon_entropy < self.min_shannon_entropy and len(non_pool_holders) >= 5:
            is_healthy = False
            failure_reason = (
                f"Holder Shannon Entropy too low ({shannon_entropy:.2f} < {self.min_shannon_entropy:.2f}). "
                f"Cluster analysis reveals synthetic distribution."
            )

        return HolderDistribution(
            mint_address=token_mint,
            top10_aggregate_pct=top10_pct,
            max_single_pct=max_single_pct,
            shannon_entropy=shannon_entropy,
            gini_coefficient=gini,
            total_holders_sampled=len(non_pool_holders),
            is_healthy=is_healthy,
            failure_reason=failure_reason,
        )

    def _calculate_gini(self, array: np.ndarray) -> float:
        """
        Calculates Gini inequality coefficient across array of holdings.
        Values close to 0 mean equal distribution, values near 1 mean extreme inequality.
        """
        if len(array) == 0:
            return 0.0
        array = np.sort(array)
        index = np.arange(1, array.shape[0] + 1)
        n = array.shape[0]
        return float(((np.sum((2 * index - n - 1) * array)) / (n * np.sum(array))))
