"""
Domain Types and Typed Data Structures
======================================
Defines strongly typed data classes and enumerations representing tokens,
order book liquidity, risk scores, trade signals, and open positions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class PoolType(str, Enum):
    """Decentralized Exchange AMM / Bonding Curve Architecture."""
    RAYDIUM_V4 = "RAYDIUM_V4"
    RAYDIUM_CPMM = "RAYDIUM_CPMM"
    PUMP_FUN = "PUMP_FUN"
    ORCA_WHIRLPOOL = "ORCA_WHIRLPOOL"
    METEORA_DLMM = "METEORA_DLMM"
    UNKNOWN = "UNKNOWN"


class TradeAction(str, Enum):
    """Trading action decisions emitted by the adaptive decision engine."""
    BUY = "BUY"
    HOLD = "HOLD"
    SCALE_OUT = "SCALE_OUT"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    STAGNATION_CUT = "STAGNATION_CUT"
    DRAWDOWN_HALT = "DRAWDOWN_HALT"


@dataclass
class PoolDetectionEvent:
    """Event emitted upon sub-millisecond detection of a new liquidity pool or injection."""
    pool_address: str
    token_mint: str
    base_mint: str
    quote_mint: str
    pool_type: PoolType
    initial_sol_liquidity: float
    signature: str
    detected_at: float = field(default_factory=time.time)


@dataclass
class TokenSecurityAudit:
    """Multi-tier security verification for token mint and authorities."""
    mint_address: str
    mint_authority: Optional[str]
    freeze_authority: Optional[str]
    lp_burned: bool
    lp_burn_pct: float
    is_token_2022: bool
    transfer_fee_bps: int
    hard_stops_passed: bool
    failure_reason: Optional[str] = None


@dataclass
class HolderDistribution:
    """Entropy and concentration metrics of non-pool token holders."""
    mint_address: str
    top10_aggregate_pct: float
    max_single_pct: float
    shannon_entropy: float
    gini_coefficient: float
    total_holders_sampled: int
    is_healthy: bool
    failure_reason: Optional[str] = None


@dataclass
class LiquidityMetrics:
    """Real-time liquidity depth and valuation health."""
    pool_address: str
    sol_reserves: float
    token_reserves: float
    price_sol: float
    fdv_sol: float
    lp_to_mc_ratio: float
    is_sufficient: bool
    failure_reason: Optional[str] = None


@dataclass
class SimulationResult:
    """Pre-flight transaction simulation outcome for honeypot and tax verification."""
    token_mint: str
    buy_amount_sol: float
    simulated_tokens_received: float
    simulated_sol_returned: float
    roundtrip_loss_pct: float
    transfer_tax_detected: bool
    is_honeypot: bool
    simulation_error: Optional[str] = None


@dataclass
class ComprehensiveRiskScore:
    """Aggregated multi-factor risk assessment."""
    token_mint: str
    composite_score: float  # 0.0 (lethal) to 100.0 (pristine)
    security_audit: TokenSecurityAudit
    holder_distribution: HolderDistribution
    liquidity_metrics: LiquidityMetrics
    simulation_result: Optional[SimulationResult]
    passed_all_filters: bool
    rejection_reasons: List[str] = field(default_factory=list)


@dataclass
class MomentumMetrics:
    """Order flow inflow velocity and retail momentum indicators."""
    token_mint: str
    window_seconds: int
    unique_buyers_count: int
    unique_sellers_count: int
    total_transactions: int
    buy_volume_sol: float
    sell_volume_sol: float
    volume_delta_sol: float
    buyer_growth_percentile: float
    is_90th_percentile: bool


@dataclass
class VolatilityMetrics:
    """Real-time volatility and ATR metrics."""
    token_mint: str
    std_1m_returns: float
    atr_sol: float
    current_price_sol: float
    regime: str  # 'LOW', 'NORMAL', 'HIGH', 'EXTREME'
    volatility_scale_factor: float


@dataclass
class PositionSizeRecommendation:
    """Volatility and depth-adjusted capital sizing."""
    token_mint: str
    wallet_liquid_sol: float
    raw_kelly_fraction: float
    volatility_adjusted_fraction: float
    depth_cap_sol: float
    final_allocation_pct: float
    allocated_sol: float
    rationale: str


@dataclass
class OpenPosition:
    """State tracking for an active trading position."""
    token_mint: str
    pool_address: str
    pool_type: PoolType
    entry_price_sol: float
    current_price_sol: float
    peak_price_sol: float
    tokens_amount: int
    sol_invested: float
    entry_timestamp: float
    trailing_stop_price: float
    scaled_out_count: int = 0
    consecutive_exhaustions: int = 0
    is_active: bool = True
    realized_pnl_sol: float = 0.0

    @property
    def unrealized_pnl_pct(self) -> float:
        """Calculate percentage unrealized gain/loss."""
        if self.entry_price_sol <= 0:
            return 0.0
        return ((self.current_price_sol - self.entry_price_sol) / self.entry_price_sol) * 100.0

    @property
    def unrealized_pnl_sol(self) -> float:
        """Calculate approximate unrealized SOL gain/loss."""
        current_value = (self.tokens_amount * self.current_price_sol)
        return current_value - self.sol_invested


@dataclass
class ClusterCongestionState:
    """Solana network cluster health telemetry."""
    tps: float
    mean_slot_time_ms: float
    tx_failure_rate: float
    is_congested: bool
    throttle_multiplier: float
