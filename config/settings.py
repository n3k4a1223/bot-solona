"""
Production Configuration Management for Solana Adaptive Trading Bot
====================================================================
Leverages Pydantic Settings for runtime validation, type coercion, and
secure environment secret extraction.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger("solana_bot.config")


class BotConfig(BaseSettings):
    """Unified application settings with dynamic risk, execution, and RPC configurations."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -------------------------------------------------------------------------
    # 1. Operational Mode & Credentials
    # -------------------------------------------------------------------------
    DRY_RUN: bool = Field(
        default=True,
        description="When True, transactions are simulated and logged without submitting actual SOL/tokens to mainnet.",
    )
    WALLET_PRIVATE_KEY: str = Field(
        default="",
        description="Base58-encoded private key or JSON byte array for transaction signing.",
    )

    # -------------------------------------------------------------------------
    # 2. Multi-RPC Load Balancer Endpoints
    # -------------------------------------------------------------------------
    PRIMARY_RPC_HTTP: str = Field(
        default="https://api.mainnet-beta.solana.com",
        description="Primary low-latency RPC HTTP endpoint (e.g., Helius / QuickNode / Triton).",
    )
    PRIMARY_RPC_WS: str = Field(
        default="wss://api.mainnet-beta.solana.com",
        description="Primary low-latency RPC WebSocket endpoint for logsSubscribe.",
    )
    SECONDARY_RPC_HTTP: Optional[str] = Field(
        default="https://solana-api.projectserum.com",
        description="Secondary failover RPC HTTP endpoint.",
    )
    SECONDARY_RPC_WS: Optional[str] = Field(
        default=None,
        description="Secondary failover RPC WebSocket endpoint.",
    )
    FALLBACK_RPC_HTTP: Optional[str] = Field(
        default="https://rpc.ankr.com/solana",
        description="Tertiary fallback RPC HTTP endpoint.",
    )
    FALLBACK_RPC_WS: Optional[str] = Field(
        default=None,
        description="Tertiary fallback RPC WebSocket endpoint.",
    )

    RPC_TIMEOUT_SECONDS: float = Field(
        default=4.0,
        description="HTTP RPC request timeout before routing to next healthiest endpoint.",
    )
    RPC_MAX_RETRIES: int = Field(
        default=3,
        description="Maximum retry attempts per logical RPC call across pool endpoints.",
    )
    RPC_HEALTH_CHECK_INTERVAL_SECONDS: float = Field(
        default=15.0,
        description="Interval between active ping and slot latency health evaluations.",
    )

    # -------------------------------------------------------------------------
    # 3. Execution Routing & MEV (Jupiter v6 & Jito)
    # -------------------------------------------------------------------------
    JUPITER_API_URL: str = Field(
        default="https://api.jup.ag/swap/v1",
        description="Jupiter Swap API v1 base URL.",
    )
    MAX_SLIPPAGE_BPS: int = Field(
        default=500,
        description="Maximum allowable slippage in basis points (500 bps = 5.0% for fast guaranteed fills).",
    )
    USE_JITO_MEV: bool = Field(
        default=True,
        description="Enable Jito Block Engine bundles for MEV protection and guaranteed atomic inclusion.",
    )
    JITO_BLOCK_ENGINE_URL: str = Field(
        default="https://ny.mainnet.block-engine.jito.wtf",
        description="Jito Block Engine JSON-RPC endpoint.",
    )
    JITO_TIP_FLOOR_URL: str = Field(
        default="https://bundles.jito.wtf/api/v1/bundles/tip_floor",
        description="Jito dynamic tip floor API for real-time congestion pricing.",
    )
    JITO_DEFAULT_TIP_PERCENTILE: int = Field(
        default=75,
        description="Target tip percentile during normal congestion (50th, 75th, 95th).",
    )
    JITO_MIN_TIP_LAMPORTS: int = Field(
        default=100_000,
        description="Minimum Jito tip in lamports (0.0001 SOL).",
    )
    JITO_MAX_TIP_LAMPORTS: int = Field(
        default=5_000_000,
        description="Hard ceiling for dynamic Jito tip in lamports (0.005 SOL) to prevent tip overshoot.",
    )

    # -------------------------------------------------------------------------
    # 4. Multi-Tier Anti-Scam & Liquidity Depth Filters
    # -------------------------------------------------------------------------
    REQUIRE_LP_BURN: bool = Field(
        default=True,
        description="Mandate LP tokens to be burned (e.g. to dead address) or verifiably locked.",
    )
    REJECT_TOKEN_2022_TAX: bool = Field(
        default=True,
        description="Reject Token-2022 tokens configured with non-zero transfer fee extensions.",
    )
    MIN_LP_SOL_RESERVES: float = Field(
        default=2.0,
        description="Minimum pool SOL reserves required before evaluating entry eligibility (2.0 SOL allows $3k-$15k MC gems).",
    )
    MIN_LP_TO_MC_RATIO: float = Field(
        default=0.08,
        description="Minimum initial LP-to-FDV ratio (8%). Reject disproportionately thin pools.",
    )
    MAX_TOP10_HOLDER_PERCENT: float = Field(
        default=20.0,
        description="Maximum aggregate percentage of total supply held by top 10 non-pool wallets.",
    )
    MAX_SINGLE_HOLDER_PERCENT: float = Field(
        default=4.0,
        description="Maximum percentage of total supply held by any single non-pool private wallet.",
    )
    MIN_HOLDER_SHANNON_ENTROPY: float = Field(
        default=2.2,
        description="Minimum Shannon entropy of top holder distribution; penalizes wallet clustering.",
    )
    MAX_SIMULATION_ROUNDTRIP_LOSS_PCT: float = Field(
        default=3.0,
        description="Maximum allowed simulated round-trip loss (%) before flagging as hidden fee / honeypot.",
    )

    # -------------------------------------------------------------------------
    # 5. Adaptive Entry & Momentum Systems
    # -------------------------------------------------------------------------
    MAX_ACTIVE_POSITIONS: int = Field(
        default=2,
        description="Maximum concurrent open positions. Set to 2 concurrent positions (2 coins opened simultaneously).",
    )
    SNIPER_DELAY_SECONDS: float = Field(
        default=0.0,
        description="Sniper execution delay in seconds (0.0s for instant entry).",
    )
    TARGET_TAKE_PROFIT_PCT: float = Field(
        default=100.0,
        description="Target profit percentage to trigger full exit (100.0% = 1X profit doubler, $5 -> $10).",
    )
    TARGET_BUY_USD: float = Field(
        default=5.0,
        description="Fixed entry trade size in USD ($5.00 per trade).",
    )
    MIN_MARKET_CAP_USD: float = Field(
        default=1500.0,
        description="Minimum market cap in USD to qualify for token buy ($1,500 allows all newly listed gems at lowest entry price).",
    )
    MAX_MARKET_CAP_USD: float = Field(
        default=500000.0,
        description="Maximum market cap in USD to qualify for token buy ($500,000).",
    )
    REQUIRE_MATCHING_WEBSITE: bool = Field(
        default=False,
        description="Mandate token has a live website whose domain name matches the token name. Set to False so newly listed pump.fun/raydium tokens can be sniped.",
    )
    REJECT_MAJOR_COIN_CLONES: bool = Field(
        default=True,
        description="Reject tokens cloning or impersonating major cryptocurrencies (BTC, ETH, SOL) or major tech stocks.",
    )
    MIN_POSITION_PCT: float = Field(
        default=0.03,
        description="Lower bound of dynamic portfolio capital allocation (3% of liquid balance).",
    )
    MAX_POSITION_PCT: float = Field(
        default=0.08,
        description="Upper bound of dynamic portfolio capital allocation (8% of liquid balance).",
    )
    KELLY_FRACTION: float = Field(
        default=0.35,
        description="Fractional Kelly scale factor (0.35 = 35% Kelly) to minimize gambler's ruin risk.",
    )
    MOMENTUM_PERCENTILE_THRESHOLD: float = Field(
        default=90.0,
        description="Threshold percentile for unique buyer inflow velocity over rolling 3-min window.",
    )
    INFLOW_WINDOW_SECONDS: int = Field(
        default=180,
        description="Rolling window in seconds to compute retail buyer velocity and order flow delta.",
    )
    MIN_UNIQUE_BUYERS_3M: int = Field(
        default=1,
        description="Minimum unique trader wallets/buyers before buy entry is approved (1 allows instant entry).",
    )
    BUY_VELOCITY_WEIGHT: float = Field(
        default=1.25,
        description="Multiplier applied to position sizing when inflow velocity is exceptionally high.",
    )

    # -------------------------------------------------------------------------
    # 6. Dynamic Exit Engine & Volatility Parameters
    # -------------------------------------------------------------------------
    ATR_MULTIPLIER: float = Field(
        default=2.0,
        description="Dynamic ATR multiplier used to compute trailing stop distance.",
    )
    VOLATILITY_LOOKBACK_PERIODS: int = Field(
        default=15,
        description="Number of 1-minute discrete return intervals for standard deviation calculation.",
    )
    CONSECUTIVE_EXHAUSTION_INTERVALS: int = Field(
        default=2,
        description="Consecutive intervals of sell-volume delta exceeding buy-volume before scaling out.",
    )
    STAGNATION_TIMEOUT_SECONDS_BASE: int = Field(
        default=15,
        description="Fast 15-second stagnation timeout to reclaim capital if token stays flat.",
    )
    MIN_VOLUME_MULTIPLIER_BASELINE: float = Field(
        default=2.0,
        description="Minimum trade volume relative to median baseline required to maintain position.",
    )

    # -------------------------------------------------------------------------
    # 7. Automated Capital Protection & Safety Circuit Breakers
    # -------------------------------------------------------------------------
    MAX_DAILY_DRAWDOWN_PCT: float = Field(
        default=30.0,
        description="Master circuit breaker trip threshold: 30% daily portfolio peak drawdown halts trading.",
    )
    CLUSTER_FAIL_RATE_THRESHOLD: float = Field(
        default=0.25,
        description="Solana cluster transaction failure rate (25%) above which entries are throttled.",
    )
    CLUSTER_TPS_MIN_THRESHOLD: float = Field(
        default=1400.0,
        description="Cluster TPS threshold below which sizing is defensively halved.",
    )

    def get_http_endpoints(self) -> List[str]:
        """Returns non-empty HTTP RPC endpoints prioritized by order."""
        endpoints = [self.PRIMARY_RPC_HTTP]
        if self.SECONDARY_RPC_HTTP:
            endpoints.append(self.SECONDARY_RPC_HTTP)
        if self.FALLBACK_RPC_HTTP:
            endpoints.append(self.FALLBACK_RPC_HTTP)
        return [ep for ep in endpoints if ep and ep.strip()]

    def get_ws_endpoints(self) -> List[str]:
        """Returns non-empty WebSocket RPC endpoints prioritized by order."""
        endpoints = [self.PRIMARY_RPC_WS]
        if self.SECONDARY_RPC_WS:
            endpoints.append(self.SECONDARY_RPC_WS)
        if self.FALLBACK_RPC_WS:
            endpoints.append(self.FALLBACK_RPC_WS)
        return [ep for ep in endpoints if ep and ep.strip()]


_config_instance: Optional[BotConfig] = None


def get_config() -> BotConfig:
    """Singleton getter for BotConfig."""
    global _config_instance
    if _config_instance is None:
        _config_instance = BotConfig()
    return _config_instance
