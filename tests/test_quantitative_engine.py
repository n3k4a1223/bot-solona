"""
Comprehensive Quantitative Test Suite
=====================================
Validates all mathematical models, anti-scam hard stops, entropy calculations,
modified Kelly Criterion, ATR trailing stops, and circuit breakers.
"""

import time
import numpy as np
import pytest
from solders.keypair import Keypair

from analytics.momentum_tracker import MomentumTracker
from analytics.position_sizer import AdaptivePositionSizer
from analytics.volatility_engine import VolatilityEngine
from core.rpc_balancer import MultiRPCBalancer
from core.types import (
    ComprehensiveRiskScore,
    HolderDistribution,
    LiquidityMetrics,
    MomentumMetrics,
    OpenPosition,
    PoolType,
    TokenSecurityAudit,
    TradeAction,
    VolatilityMetrics,
)
from filters.holder_analyzer import HolderDistributionAnalyzer
from filters.liquidity_health import LiquidityHealthFilter
from filters.security_filter import TokenSecurityFilter
from risk.circuit_breaker import CircuitBreakerManager
from risk.exit_engine import DynamicExitEngine


# =============================================================================
# 1. Security Filter Unit Tests
# =============================================================================

def test_mint_and_freeze_authority_hard_stop():
    """Verify that any non-null mint or freeze authority is immediately rejected."""
    rpc = MultiRPCBalancer(endpoints=["https://api.mainnet-beta.solana.com"], dry_run=True)
    sec_filter = TokenSecurityFilter(rpc)

    # 1. Mint authority present (COption = 1 at offset 0)
    data_with_mint_auth = bytearray(82)
    data_with_mint_auth[0] = 1  # Some(Pubkey)
    data_with_mint_auth[4:36] = b"\x01" * 32
    mint_auth, freeze_auth, supply, dec = sec_filter._parse_mint_data(bytes(data_with_mint_auth))
    assert mint_auth is not None
    assert freeze_auth is None

    # 2. Freeze authority present (COption = 1 at offset 46)
    data_with_freeze_auth = bytearray(82)
    data_with_freeze_auth[46] = 1  # Some(Pubkey)
    data_with_freeze_auth[50:82] = b"\x02" * 32
    mint_auth2, freeze_auth2, _, _ = sec_filter._parse_mint_data(bytes(data_with_freeze_auth))
    assert mint_auth2 is None
    assert freeze_auth2 is not None

    # 3. Clean token (Both None)
    clean_data = bytearray(82)
    clean_mint, clean_freeze, _, _ = sec_filter._parse_mint_data(bytes(clean_data))
    assert clean_mint is None
    assert clean_freeze is None


def test_token_2022_tax_extension_parsing():
    """Verify detection of Token-2022 TransferFeeConfig extension."""
    rpc = MultiRPCBalancer(endpoints=["https://api.mainnet-beta.solana.com"], dry_run=True)
    sec_filter = TokenSecurityFilter(rpc)

    # Synthesize Token-2022 mint layout with extension type 1 (TransferFeeConfig)
    data = bytearray(82)
    data.append(1)  # Account type
    # Append TLV: Type=1 (TransferFeeConfig), Length=84
    import struct
    data.extend(struct.pack("<HH", 1, 84))
    # Fill extension body with fee = 500 bps (5.0%)
    ext_body = bytearray(84)
    # Put fee_bps at offset 76..78
    struct.pack_into("<H", ext_body, 76, 500)
    data.extend(ext_body)

    fee_bps = sec_filter._parse_token_2022_transfer_fee(bytes(data))
    assert fee_bps == 500, f"Expected 500 bps, got {fee_bps}"


# =============================================================================
# 2. Holder Distribution Entropy Tests
# =============================================================================

def test_holder_entropy_and_concentration():
    """Verify Shannon entropy and Gini calculations for decentralized vs clustered wallets."""
    rpc = MultiRPCBalancer(endpoints=["https://api.mainnet-beta.solana.com"], dry_run=True)
    analyzer = HolderDistributionAnalyzer(rpc, max_top10_percent=20.0, max_single_percent=4.0, min_shannon_entropy=2.2)

    # Clustered cabal holdings (Top wallet holds 12%, top 10 hold 45%)
    clustered_holdings = np.array([120, 80, 50, 40, 35, 30, 25, 25, 25, 20], dtype=np.float64)
    probs_c = clustered_holdings / np.sum(clustered_holdings)
    entropy_c = float(-np.sum(probs_c * np.log(probs_c)))
    gini_c = analyzer._calculate_gini(clustered_holdings)

    # Decentralized organic holdings (Equal distribution across 15 wallets)
    organic_holdings = np.array([25, 22, 20, 18, 16, 15, 14, 12, 11, 10, 9, 8, 7, 6, 5], dtype=np.float64)
    probs_o = organic_holdings / np.sum(organic_holdings)
    entropy_o = float(-np.sum(probs_o * np.log(probs_o)))
    gini_o = analyzer._calculate_gini(organic_holdings)

    assert entropy_o > entropy_c, "Organic distribution must have higher Shannon entropy than clustered"
    assert gini_c > gini_o, "Clustered distribution must have higher Gini inequality than organic"
    assert entropy_o >= 2.2, f"Organic entropy {entropy_o:.2f} should clear threshold 2.2"


# =============================================================================
# 3. Dynamic Liquidity Health Tests
# =============================================================================

@pytest.mark.asyncio
async def test_liquidity_health_ratio():
    """Verify LP-to-MC ratio mathematical valuation logic."""
    rpc = MultiRPCBalancer(endpoints=["https://api.mainnet-beta.solana.com"], dry_run=True)
    liq_filter = LiquidityHealthFilter(rpc, min_sol_reserves=15.0, min_lp_to_mc_ratio=0.08)

    # 1. Under minimum SOL reserves
    res_thin_sol = await liq_filter.evaluate_pool_liquidity("PoolAddr", "TokenMint", sol_reserves=5.0, token_reserves=500_000)
    assert not res_thin_sol.is_sufficient
    assert "Insufficient SOL liquidity" in (res_thin_sol.failure_reason or "")

    # 2. Healthy liquidity pool (45 SOL reserves, 800M tokens in pool out of 1B supply)
    res_healthy = await liq_filter.evaluate_pool_liquidity("PoolAddr", "TokenMint", sol_reserves=45.0, token_reserves=800_000_000)
    assert res_healthy.is_sufficient
    assert res_healthy.lp_to_mc_ratio >= 0.08, f"LP/MC ratio {res_healthy.lp_to_mc_ratio} should be >= 0.08"


# =============================================================================
# 4. Momentum Velocity & Wash Trading Filter Tests
# =============================================================================

def test_momentum_tracker_and_wash_trading():
    """Verify wash trading / spoofing penalty vs genuine organic retail velocity."""
    tracker = MomentumTracker(window_seconds=180, min_unique_buyers=10, percentile_threshold=90.0)

    # Test Spoofing: 1 single wallet makes 20 buy transactions
    for i in range(20):
        tracker.record_swap("SpoofedToken", "SpooferWallet_999", is_buy=True, sol_amount=0.5, token_amount=1000, signature=f"sig_{i}")

    spoofed_metrics = tracker.evaluate_momentum("SpoofedToken")
    assert not spoofed_metrics.is_90th_percentile, "Spoofed single-wallet volume must NOT trigger entry"

    # Test Genuine Retail Velocity: 18 unique distinct buyer wallets
    for i in range(18):
        tracker.record_swap("RetailToken", f"RetailBuyer_{i:02d}", is_buy=True, sol_amount=1.2, token_amount=5000, signature=f"ret_sig_{i}")

    retail_metrics = tracker.evaluate_momentum("RetailToken")
    assert retail_metrics.unique_buyers_count >= 10
    assert retail_metrics.buyer_growth_percentile >= 90.0
    assert retail_metrics.is_90th_percentile, "Organic retail velocity must trigger 90th percentile entry"


# =============================================================================
# 5. Adaptive Position Sizing (Modified Kelly + Depth Capped)
# =============================================================================

def test_adaptive_position_sizer():
    """Verify dynamic Kelly sizing, volatility normalization, and 3% - 8% clamping."""
    sizer = AdaptivePositionSizer(min_position_pct=0.03, max_position_pct=0.08, kelly_fraction=0.35)

    risk_score = ComprehensiveRiskScore(
        token_mint="TestToken",
        composite_score=92.0,
        security_audit=TokenSecurityAudit("TestToken", None, None, True, 100.0, False, 0, True),
        holder_distribution=HolderDistribution("TestToken", 15.0, 3.0, 2.6, 0.2, 15, True),
        liquidity_metrics=LiquidityMetrics("Pool", 50.0, 800_000_000, 0.00000006, 62.5, 1.6, True),
        simulation_result=None,
        passed_all_filters=True,
    )

    momentum = MomentumMetrics("TestToken", 180, 18, 2, 20, 25.0, 2.0, 23.0, 95.0, True)

    # Normal volatility regime (scale factor 1.0)
    vol_normal = VolatilityMetrics("TestToken", 0.03, 0.000002, 0.00000006, "NORMAL", 1.0)
    # 1. Deep pool test (1,000 SOL reserves): Depth cap is 15 SOL, so Kelly sizing operates freely within 3% - 8%
    rec_deep = sizer.calculate_size(
        wallet_balance_sol=100.0,
        pool_sol_reserves=1000.0,
        risk_score=risk_score,
        volatility=vol_normal,
        momentum=momentum,
    )
    assert 0.03 <= rec_deep.final_allocation_pct <= 0.08, "Deep pool allocation must be strictly bounded between 3% and 8%"

    # 2. Thin pool test (50 SOL reserves): Depth cap (0.75 SOL) safely overrides desired size to prevent slippage
    rec_thin = sizer.calculate_size(
        wallet_balance_sol=100.0,
        pool_sol_reserves=50.0,
        risk_score=risk_score,
        volatility=vol_normal,
        momentum=momentum,
    )
    assert rec_thin.allocated_sol <= 50.0 * 0.015, "Order size must respect order-book depth safety cap"
    assert rec_thin.allocated_sol == 0.75


def test_micro_capital_position_sizer():
    """Verify autonomous micro-capital scaling for small accounts (~$5 / 0.03 SOL)."""
    sizer = AdaptivePositionSizer(
        min_position_pct=0.03,
        max_position_pct=0.08,
        kelly_fraction=0.35,
        micro_cap_threshold_sol=0.15,
        gas_reserve_sol=0.005,
    )

    risk_score = ComprehensiveRiskScore(
        token_mint="MicroToken",
        composite_score=90.0,
        security_audit=TokenSecurityAudit("MicroToken", None, None, True, 100.0, False, 0, True),
        holder_distribution=HolderDistribution("MicroToken", 12.0, 2.5, 2.8, 0.15, 20, True),
        liquidity_metrics=LiquidityMetrics("Pool", 40.0, 500_000_000, 0.00000008, 62.5, 1.6, True),
        simulation_result=None,
        passed_all_filters=True,
    )
    momentum = MomentumMetrics("MicroToken", 180, 16, 1, 17, 20.0, 1.0, 19.0, 96.0, True)
    vol_normal = VolatilityMetrics("MicroToken", 0.03, 0.000002, 0.00000008, "NORMAL", 1.0)

    # 1. Underfunded wallet (< 0.010 SOL)
    rec_underfunded = sizer.calculate_size(
        wallet_balance_sol=0.008,
        pool_sol_reserves=40.0,
        risk_score=risk_score,
        volatility=vol_normal,
        momentum=momentum,
    )
    assert rec_underfunded.allocated_sol == 0.0
    assert "insufficient" in rec_underfunded.rationale.lower()

    # 2. $5 account (0.03 SOL): Should allocate aggressively (~0.015 - 0.020 SOL) with fee reserve preserved
    rec_micro = sizer.calculate_size(
        wallet_balance_sol=0.030,
        pool_sol_reserves=40.0,
        risk_score=risk_score,
        volatility=vol_normal,
        momentum=momentum,
    )
    assert rec_micro.allocated_sol >= 0.010, "Micro trade must exceed minimal execution threshold"
    assert rec_micro.allocated_sol <= (0.030 - 0.005), "Must preserve 0.005 SOL fee reserve buffer"
    assert "MICRO-CAP AGGRESSIVE MODE" in rec_micro.rationale



# =============================================================================
# 6. Dynamic Exit Engine Tests (Trailing Stop & Exhaustion Scale-Out)
# =============================================================================

def test_exit_engine_trailing_stop_and_scale_out():
    """Verify volatility trailing stop ratchets and buyer exhaustion scale-outs."""
    exit_engine = DynamicExitEngine(atr_multiplier=2.0, consecutive_exhaustion_intervals=2)

    entry_price = 0.0010
    pos = OpenPosition(
        token_mint="TestToken",
        pool_address="PoolAddr",
        pool_type=PoolType.RAYDIUM_V4,
        entry_price_sol=entry_price,
        current_price_sol=entry_price,
        peak_price_sol=entry_price,
        tokens_amount=100_000,
        sol_invested=100.0,
        entry_timestamp=time.time(),
        trailing_stop_price=entry_price * 0.90,  # 10% stop initial
    )

    vol = VolatilityMetrics("TestToken", 0.03, entry_price * 0.04, entry_price, "NORMAL", 1.0)
    mom_neutral = MomentumMetrics("TestToken", 180, 5, 2, 7, 5.0, 2.0, 3.0, 70.0, False)

    # 1. Price runs up to 0.0015 (+50%)
    action1, _, _ = exit_engine.evaluate_position_exit(pos, 0.0015, vol, mom_neutral)
    assert action1 == TradeAction.HOLD
    assert pos.peak_price_sol == 0.0015
    assert pos.trailing_stop_price > entry_price, "Trailing stop must ratchet UP above entry price"

    # 2. Buyer volume exhaustion: 2 intervals of sell dominance
    mom_exhausted = MomentumMetrics("TestToken", 180, 2, 8, 10, 1.0, 8.0, -7.0, 40.0, False)
    exit_engine.evaluate_position_exit(pos, 0.0015, vol, mom_exhausted)
    action_scale, reason_scale, fraction = exit_engine.evaluate_position_exit(pos, 0.0015, vol, mom_exhausted)
    assert action_scale == TradeAction.SCALE_OUT, "Exhaustion must trigger SCALE_OUT"
    assert fraction == 0.50, "Initial scale-out must derisk 50% of position"

    # 3. Pullback breaches ratcheted stop
    stop_price = pos.trailing_stop_price
    action_stop, reason_stop, frac_stop = exit_engine.evaluate_position_exit(pos, stop_price - 0.0001, vol, mom_neutral)
    assert action_stop == TradeAction.STOP_LOSS, "Stop breach must trigger STOP_LOSS"
    assert frac_stop == 1.0, "Stop loss must exit 100% of remaining position"


def test_exit_engine_100_percent_take_profit_doubler():
    """Verify master take-profit triggers 100% exit when position doubles (+100% PnL)."""
    exit_engine = DynamicExitEngine(target_take_profit_pct=100.0)

    entry_price = 0.0010
    pos = OpenPosition(
        token_mint="DoublerToken",
        pool_address="PoolAddr",
        pool_type=PoolType.RAYDIUM_V4,
        entry_price_sol=entry_price,
        current_price_sol=entry_price,
        peak_price_sol=entry_price,
        tokens_amount=100_000,
        sol_invested=20.0,  # e.g., $20 trade
        entry_timestamp=time.time(),
        trailing_stop_price=entry_price * 0.90,
    )
    vol = VolatilityMetrics("DoublerToken", 0.03, entry_price * 0.04, entry_price, "NORMAL", 1.0)
    mom = MomentumMetrics("DoublerToken", 180, 20, 2, 22, 25.0, 2.0, 23.0, 95.0, True)

    # 1. Price doubles: from 0.0010 -> 0.0020 (+100% gain, $20 -> $40)
    double_price = entry_price * 2.0
    action, reason, fraction = exit_engine.evaluate_position_exit(pos, double_price, vol, mom)

    assert action == TradeAction.TAKE_PROFIT, f"Expected TAKE_PROFIT on 2x price, got {action}"
    assert fraction == 1.0, "Must exit 100% of position to rotate capital"
    assert "TARGET DOUBLED" in reason



# =============================================================================
# 7. Safety Circuit Breaker Tests
# =============================================================================

def test_daily_drawdown_governor():
    """Verify master circuit breaker trips on 6% daily peak equity drawdown."""
    rpc = MultiRPCBalancer(endpoints=["https://api.mainnet-beta.solana.com"], dry_run=True)
    governor = CircuitBreakerManager(rpc, max_daily_drawdown_pct=6.0)

    # Initial equity: 100 SOL
    governor.update_portfolio_equity(current_liquid_sol=100.0, unrealized_pnl_sol=0.0)
    assert governor.peak_equity_sol == 100.0

    # Equity grows to 110 SOL
    governor.update_portfolio_equity(current_liquid_sol=105.0, unrealized_pnl_sol=5.0)
    assert governor.peak_equity_sol == 110.0

    # Small drawdown: 106 SOL (3.6% drawdown) -> should NOT trip
    is_tripped, dd = governor.update_portfolio_equity(current_liquid_sol=104.0, unrealized_pnl_sol=2.0)
    assert not is_tripped
    assert dd < 6.0

    # Large drawdown: 102 SOL (7.2% drawdown >= 6.0%) -> must TRIP!
    is_tripped2, dd2 = governor.update_portfolio_equity(current_liquid_sol=102.0, unrealized_pnl_sol=0.0)
    assert is_tripped2, "Daily Drawdown Governor must trip when drawdown exceeds 6.0%"
    assert governor.is_drawdown_tripped


# =============================================================================
# 8. User Strategy Specification Tests: $5 Sizing, Website Match & Anti-Clones
# =============================================================================

def test_fixed_5_dollar_position_sizer():
    """Verify that trades size to exactly $5.00 USD in SOL while preserving gas reserve."""
    # SOL price = $120 -> $5.00 is 5 / 120 = 0.04167 SOL
    sizer = AdaptivePositionSizer(
        target_buy_usd=5.0,
        sol_price_usd=120.0,
        gas_reserve_sol=0.005,
    )
    score = ComprehensiveRiskScore(
        token_mint="TestMint",
        composite_score=95.0,
        security_audit=TokenSecurityAudit("TestMint", None, None, True, 100.0, False, 0, True),
        holder_distribution=HolderDistribution("TestMint", 10.0, 2.0, 2.8, 0.4, 100, True),
        liquidity_metrics=LiquidityMetrics("P", 50.0, 1_000_000, 0.00005, 50.0, 0.15, True),
        simulation_result=None,
        passed_all_filters=True,
    )
    vol = VolatilityMetrics("TestMint", 0.02, 0.000001, 0.00005, "NORMAL", 1.0)
    mom = MomentumMetrics("TestMint", 180, 25, 2, 27, 30.0, 2.0, 28.0, 95.0, True)

    # Wallet with 0.4229 SOL (plenty for $5 trade)
    rec = sizer.calculate_size(
        wallet_balance_sol=0.4229,
        pool_sol_reserves=50.0,
        risk_score=score,
        volatility=vol,
        momentum=mom,
    )

    expected_sol = 5.0 / 120.0  # ~0.04167 SOL
    assert abs(rec.allocated_sol - expected_sol) < 0.001
    assert "FIXED $5.00 USD MODE" in rec.rationale


def test_metadata_website_match_and_rejection():
    """Verify that tokens without a dedicated matching website are strictly rejected."""
    from filters.metadata_audit import TokenMetadataAuditor

    # 1. Matching dedicated website -> PASS
    has_web, matches, fail = TokenMetadataAuditor.verify_website_match("PepeCat", "PEPECAT", "https://pepecat.io")
    assert has_web and matches
    assert fail is None

    # 2. Matching website with TLD variations -> PASS
    has_web2, matches2, fail2 = TokenMetadataAuditor.verify_website_match("Bonk", "BONK", "https://www.bonkcoin.com")
    assert has_web2 and matches2

    # 3. Mismatched domain name -> REJECT
    has_web3, matches3, fail3 = TokenMetadataAuditor.verify_website_match("RandomGem", "RND", "https://unrelatedsite.com")
    assert has_web3
    assert not matches3
    assert "does not match" in fail3

    # 4. Social media redirect (telegram / twitter / linktree) -> REJECT
    has_web4, matches4, fail4 = TokenMetadataAuditor.verify_website_match("MyToken", "MTK", "https://t.me/mytokengroup")
    assert not matches4
    assert "social media or generic platform" in fail4

    # 5. Missing website -> REJECT
    has_web5, matches5, fail5 = TokenMetadataAuditor.verify_website_match("NoWebToken", "NOWEB", None)
    assert not has_web5
    assert not matches5


def test_metadata_major_clone_blacklist():
    """Verify strict rejection of any major coin or equity clones (BTC, ETH, SOL, TSLA, etc.)."""
    from filters.metadata_audit import TokenMetadataAuditor

    # Major coin clones -> MUST BE REJECTED
    clones = [
        ("BabyBitcoin", "BBTC"),
        ("Solana 2.0", "SOL2"),
        ("Wrapped ETH", "WETH"),
        ("Elon Tesla", "TSLA"),
        ("Nvidia AI", "NVDA"),
        ("Dogecoin Killer", "DOGEK"),
        ("Official Apple Token", "AAPL"),
    ]
    for name, sym in clones:
        is_clone, asset = TokenMetadataAuditor.detect_major_clone(name, sym)
        assert is_clone, f"Expected {name} to be flagged as major clone of {asset}"

    # Authentic unique tokens -> MUST PASS
    authentic = [
        ("PepeCat", "PEPECAT"),
        ("Bonk", "BONK"),
        ("Popcat", "POPCAT"),
        ("WolfWifHat", "WOLF"),
    ]
    for name, sym in authentic:
        is_clone, asset = TokenMetadataAuditor.detect_major_clone(name, sym)
        assert not is_clone, f"Authentic token {name} should NOT be flagged as clone"


@pytest.mark.asyncio
async def test_market_cap_bounds_3000_to_15000():
    """Verify that market caps below $3,000 or above $15,000 are rejected."""
    from filters.metadata_audit import TokenMetadataAuditor

    rpc = MultiRPCBalancer(endpoints=["https://api.mainnet-beta.solana.com"], dry_run=True)
    auditor = TokenMetadataAuditor(
        rpc,
        min_market_cap_usd=3000.0,
        max_market_cap_usd=15000.0,
        sol_price_usd=120.0,
    )

    # 1. Market cap below $3,000 ($1,200) -> REJECT
    # 5 SOL reserves * 2 = 10 SOL FDV * $120 = $1,200 USD
    res_low = await auditor.audit_token("LowMCMint", "PoolLow", sol_reserves=5.0, token_reserves=1_000_000)
    assert not res_low.passed
    assert "below minimum threshold" in res_low.failure_reason

    # 2. Market cap above $15,000 ($240,000) -> REJECT
    # 1000 SOL reserves * 2 = 2000 SOL FDV * $120 = $240,000 USD
    res_high = await auditor.audit_token("HighMCMint", "PoolHigh", sol_reserves=1000.0, token_reserves=1_000_000)
    assert not res_high.passed
    assert "exceeds maximum threshold" in res_high.failure_reason
