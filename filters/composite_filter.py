"""
Composite Risk Scoring Orchestrator
===================================
Coordinates multi-tier security audits, holder entropy analysis, liquidity
health calculations, and pre-flight honeypot simulations into a unified
risk assessment score.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import (
    ComprehensiveRiskScore,
    HolderDistribution,
    LiquidityMetrics,
    SimulationResult,
    TokenMetadataAudit,
    TokenSecurityAudit,
)
from filters.holder_analyzer import HolderDistributionAnalyzer
from filters.honeypot_sim import HoneypotSimulationFilter
from filters.liquidity_health import LiquidityHealthFilter
from filters.metadata_audit import TokenMetadataAuditor
from filters.security_filter import TokenSecurityFilter


class CompositeRiskEvaluator:
    """
    Executes all anti-scam, liquidity, holder distribution, and metadata checks in parallel,
    synthesizing a composite confidence score from 0.0 to 100.0.
    """

    def __init__(
        self,
        rpc_balancer: MultiRPCBalancer,
        jupiter_api_url: str = "https://quote-api.jup.ag/v6",
        min_sol_reserves: float = 15.0,
        min_lp_to_mc_ratio: float = 0.08,
        max_top10_percent: float = 20.0,
        max_single_percent: float = 4.0,
        min_shannon_entropy: float = 2.2,
        max_roundtrip_loss_pct: float = 3.0,
        min_market_cap_usd: float = 3000.0,
        max_market_cap_usd: float = 15000.0,
        sol_price_usd: float = 120.0,
        require_matching_website: bool = False,
    ):
        self.security_filter = TokenSecurityFilter(rpc_balancer)
        self.holder_analyzer = HolderDistributionAnalyzer(
            rpc_balancer,
            max_top10_percent=max_top10_percent,
            max_single_percent=max_single_percent,
            min_shannon_entropy=min_shannon_entropy,
        )
        self.liquidity_filter = LiquidityHealthFilter(
            rpc_balancer,
            min_sol_reserves=min_sol_reserves,
            min_lp_to_mc_ratio=min_lp_to_mc_ratio,
        )
        self.metadata_auditor = TokenMetadataAuditor(
            rpc_balancer,
            min_market_cap_usd=min_market_cap_usd,
            max_market_cap_usd=max_market_cap_usd,
            sol_price_usd=sol_price_usd,
            require_matching_website=require_matching_website,
        )
        self.honeypot_filter = HoneypotSimulationFilter(
            rpc_balancer,
            jupiter_api_url=jupiter_api_url,
            max_roundtrip_loss_pct=max_roundtrip_loss_pct,
        )
        self.logger = get_logger()

    async def evaluate_token(
        self,
        token_mint: str,
        pool_address: str,
        sol_reserves: float,
        token_reserves: float,
        lp_mint: Optional[str] = None,
        skip_simulation: bool = False,
    ) -> ComprehensiveRiskScore:
        """
        Executes parallel multi-tier evaluation.
        Hard stops instantly abort and assign a 0.0 risk score.
        """
        rejection_reasons: List[str] = []

        # ---------------------------------------------------------------------
        # Tier 1: Hard Stops (Mint & Freeze Authority, LP Burn, Token-2022 Tax)
        # ---------------------------------------------------------------------
        security_audit = await self.security_filter.audit_token(token_mint, lp_mint=lp_mint)
        if not security_audit.hard_stops_passed:
            rejection_reasons.append(security_audit.failure_reason or "Security hard stop failed")
            return ComprehensiveRiskScore(
                token_mint=token_mint,
                composite_score=0.0,
                security_audit=security_audit,
                holder_distribution=HolderDistribution(
                    mint_address=token_mint,
                    top10_aggregate_pct=100.0,
                    max_single_pct=100.0,
                    shannon_entropy=0.0,
                    gini_coefficient=1.0,
                    total_holders_sampled=0,
                    is_healthy=False,
                ),
                liquidity_metrics=LiquidityMetrics(
                    pool_address=pool_address,
                    sol_reserves=sol_reserves,
                    token_reserves=token_reserves,
                    price_sol=0.0,
                    fdv_sol=0.0,
                    lp_to_mc_ratio=0.0,
                    is_sufficient=False,
                ),
                simulation_result=None,
                passed_all_filters=False,
                rejection_reasons=rejection_reasons,
            )

        # ---------------------------------------------------------------------
        # Tier 2, 3 & 4: Run Liquidity Health, Holder Entropy & Metadata Concurrently
        # ---------------------------------------------------------------------
        liq_task = self.liquidity_filter.evaluate_pool_liquidity(
            pool_address=pool_address,
            token_mint=token_mint,
            sol_reserves=sol_reserves,
            token_reserves=token_reserves,
        )
        holder_task = self.holder_analyzer.analyze(token_mint=token_mint)
        meta_task = self.metadata_auditor.audit_token(
            token_mint=token_mint,
            pool_address=pool_address,
            sol_reserves=sol_reserves,
            token_reserves=token_reserves,
        )

        if not skip_simulation:
            sim_task = self.honeypot_filter.simulate_roundtrip(token_mint=token_mint)
            liq_res, holder_res, meta_res, sim_res = await asyncio.gather(liq_task, holder_task, meta_task, sim_task)
        else:
            liq_res, holder_res, meta_res = await asyncio.gather(liq_task, holder_task, meta_task)
            sim_res = None

        if not liq_res.is_sufficient and liq_res.failure_reason:
            rejection_reasons.append(liq_res.failure_reason)

        if not holder_res.is_healthy and holder_res.failure_reason:
            rejection_reasons.append(holder_res.failure_reason)

        if not meta_res.passed and meta_res.failure_reason:
            rejection_reasons.append(meta_res.failure_reason)

        if sim_res and sim_res.is_honeypot:
            rejection_reasons.append(sim_res.simulation_error or "Honeypot simulation detected exit failure")

        # ---------------------------------------------------------------------
        # Dynamic Composite Scoring Calculation (0 to 100)
        # ---------------------------------------------------------------------
        # Start at base 100, deduct points for risk attributes
        score = 100.0

        # Metadata failure hard deduct
        if not meta_res.passed:
            score -= 50.0

        # Liquidity Health contribution (max deduction 35)
        if not liq_res.is_sufficient:
            score -= 35.0
        else:
            # Reward healthy LP-to-MC ratio (e.g. > 15% is optimal)
            if liq_res.lp_to_mc_ratio < 0.12:
                score -= 10.0

        # Holder Distribution contribution (max deduction 35)
        if not holder_res.is_healthy:
            score -= 35.0
        else:
            # Penalize moderate concentration
            if holder_res.top10_aggregate_pct > 15.0:
                score -= 8.0
            if holder_res.shannon_entropy < 2.5:
                score -= 7.0

        # Simulation contribution (max deduction 30)
        if sim_res:
            if sim_res.is_honeypot:
                score -= 30.0
            elif sim_res.roundtrip_loss_pct > 2.0:
                score -= (sim_res.roundtrip_loss_pct - 2.0) * 10.0

        score = max(0.0, min(100.0, score))
        passed = (len(rejection_reasons) == 0) and (score >= 70.0)

        return ComprehensiveRiskScore(
            token_mint=token_mint,
            composite_score=score,
            security_audit=security_audit,
            holder_distribution=holder_res,
            liquidity_metrics=liq_res,
            simulation_result=sim_res,
            passed_all_filters=passed,
            metadata_audit=meta_res,
            rejection_reasons=rejection_reasons,
        )
