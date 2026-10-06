"""
Master Quantitative Solana Trading Engine Orchestrator
======================================================
Coordinates multi-RPC networking, WebSocket log ingestion, multi-tier risk
filtering, volatility-adjusted position sizing, and the dynamic exit loop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional

from analytics.momentum_tracker import MomentumTracker
from analytics.position_sizer import AdaptivePositionSizer
from analytics.volatility_engine import VolatilityEngine
from config.settings import BotConfig, get_config
from core.logger import AsyncBotLogger, get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import (
    OpenPosition,
    PoolDetectionEvent,
    PoolType,
    TradeAction,
)
from core.ws_listener import SolanaWebSocketListener
from execution.executor import TradeExecutor
from execution.jito_client import JitoMEVClient
from execution.jupiter_client import JupiterClient
from filters.composite_filter import CompositeRiskEvaluator
from risk.circuit_breaker import CircuitBreakerManager
from risk.exit_engine import DynamicExitEngine

WSOL_MINT = "So11111111111111111111111111111111111111112"


class TradingEngine:
    """
    Unified asynchronous orchestrator executing adaptive trading strategies.
    """

    def __init__(self, config: Optional[BotConfig] = None):
        self.config = config or get_config()
        self.logger: AsyncBotLogger = get_logger(dry_run=self.config.DRY_RUN)

        # Core Networking
        self.rpc_balancer = MultiRPCBalancer(
            endpoints=self.config.get_http_endpoints(),
            timeout_seconds=self.config.RPC_TIMEOUT_SECONDS,
            max_retries=self.config.RPC_MAX_RETRIES,
            dry_run=self.config.DRY_RUN,
        )

        # Execution Clients
        self.jupiter_client = JupiterClient(api_url=self.config.JUPITER_API_URL)
        self.jito_client = JitoMEVClient(
            block_engine_url=self.config.JITO_BLOCK_ENGINE_URL,
            tip_floor_url=self.config.JITO_TIP_FLOOR_URL,
            min_tip_lamports=self.config.JITO_MIN_TIP_LAMPORTS,
            max_tip_lamports=self.config.JITO_MAX_TIP_LAMPORTS,
            default_percentile=self.config.JITO_DEFAULT_TIP_PERCENTILE,
        )
        self.executor = TradeExecutor(
            rpc_balancer=self.rpc_balancer,
            jupiter_client=self.jupiter_client,
            jito_client=self.jito_client,
            private_key_str=self.config.WALLET_PRIVATE_KEY,
            dry_run=self.config.DRY_RUN,
            use_jito=self.config.USE_JITO_MEV,
        )

        # Filters and Analytics
        self.risk_evaluator = CompositeRiskEvaluator(
            rpc_balancer=self.rpc_balancer,
            jupiter_api_url=self.config.JUPITER_API_URL,
            min_sol_reserves=self.config.MIN_LP_SOL_RESERVES,
            min_lp_to_mc_ratio=self.config.MIN_LP_TO_MC_RATIO,
            max_top10_percent=self.config.MAX_TOP10_HOLDER_PERCENT,
            max_single_percent=self.config.MAX_SINGLE_HOLDER_PERCENT,
            min_shannon_entropy=self.config.MIN_HOLDER_SHANNON_ENTROPY,
            max_roundtrip_loss_pct=self.config.MAX_SIMULATION_ROUNDTRIP_LOSS_PCT,
            min_market_cap_usd=self.config.MIN_MARKET_CAP_USD,
            max_market_cap_usd=self.config.MAX_MARKET_CAP_USD,
            require_matching_website=self.config.REQUIRE_MATCHING_WEBSITE,
        )
        self.momentum_tracker = MomentumTracker(
            window_seconds=self.config.INFLOW_WINDOW_SECONDS,
            min_unique_buyers=self.config.MIN_UNIQUE_BUYERS_3M,
            percentile_threshold=self.config.MOMENTUM_PERCENTILE_THRESHOLD,
        )
        self.volatility_engine = VolatilityEngine(
            lookback_periods=self.config.VOLATILITY_LOOKBACK_PERIODS,
        )
        self.position_sizer = AdaptivePositionSizer(
            min_position_pct=self.config.MIN_POSITION_PCT,
            max_position_pct=self.config.MAX_POSITION_PCT,
            kelly_fraction=self.config.KELLY_FRACTION,
            buy_velocity_weight=self.config.BUY_VELOCITY_WEIGHT,
            target_buy_usd=self.config.TARGET_BUY_USD,
        )

        # Risk Management
        self.circuit_breaker = CircuitBreakerManager(
            rpc_balancer=self.rpc_balancer,
            max_daily_drawdown_pct=self.config.MAX_DAILY_DRAWDOWN_PCT,
            congestion_fail_rate_threshold=self.config.CLUSTER_FAIL_RATE_THRESHOLD,
            min_cluster_tps=self.config.CLUSTER_TPS_MIN_THRESHOLD,
        )
        self.exit_engine = DynamicExitEngine(
            atr_multiplier=self.config.ATR_MULTIPLIER,
            consecutive_exhaustion_intervals=self.config.CONSECUTIVE_EXHAUSTION_INTERVALS,
            base_stagnation_seconds=self.config.STAGNATION_TIMEOUT_SECONDS_BASE,
            min_volume_multiplier_baseline=self.config.MIN_VOLUME_MULTIPLIER_BASELINE,
            target_take_profit_pct=self.config.TARGET_TAKE_PROFIT_PCT,
        )

        # WebSocket Listener
        self.ws_listener = SolanaWebSocketListener(
            ws_endpoints=self.config.get_ws_endpoints(),
            on_pool_detected=self.handle_pool_detection,
        )

        # Active State Tracking
        self.active_positions: Dict[str, OpenPosition] = {}
        self.monitored_pools: Dict[str, Dict] = {}
        self.wallet_liquid_sol: float = 10.0  # Default simulation balance
        self._buy_lock = asyncio.Lock()
        self._pending_buys: int = 0
        self._running = False
        self._position_monitor_task: Optional[asyncio.Task] = None
        self._cluster_monitor_task: Optional[asyncio.Task] = None
        self._dex_market_scanner_task: Optional[asyncio.Task] = None
        self._pumpportal_ws_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Starts all background services and trading loops."""
        self.logger.print_banner()
        self._running = True

        # 1. Initialize RPC Balancer
        self.logger.log_info("Starting Multi-RPC Load Balancer...")
        await self.rpc_balancer.start()

        # 2. Query initial wallet balance
        if not self.config.DRY_RUN:
            bal = await self.rpc_balancer.get_balance(self.executor.pubkey_str)
            self.wallet_liquid_sol = bal
            self.logger.log_info(f"Live Wallet Balance: [bold green]{self.wallet_liquid_sol:.4f} SOL[/]")
            if self.wallet_liquid_sol <= 0.01:
                self.logger.log_warning(
                    f"[bold yellow]Wallet balance is currently {self.wallet_liquid_sol:.4f} SOL. "
                    f"Send trading capital to {self.executor.pubkey_str} to execute live orders.[/]"
                )
        else:
            self.logger.log_info(f"Dry-Run Initial Balance: [bold green]{self.wallet_liquid_sol:.4f} SOL[/]")

        self.circuit_breaker.set_initial_equity(self.wallet_liquid_sol)
        self.logger.liquid_balance_sol = self.wallet_liquid_sol
        self.logger.peak_equity_sol = self.wallet_liquid_sol

        # 3. Start background supervision loops
        self._cluster_monitor_task = asyncio.create_task(self._cluster_telemetry_loop())
        self._position_monitor_task = asyncio.create_task(self._position_monitoring_loop())
        self._dex_market_scanner_task = asyncio.create_task(self._dex_market_scanner_loop())
        self._pumpportal_ws_task = asyncio.create_task(self._pumpportal_ws_stream_loop())

        # 4. Start WebSocket Stream
        self.logger.log_info("Starting WebSocket DEX Log Listener...")
        await self.ws_listener.start()
        self.logger.log_success("Trading Engine successfully initialized. Awaiting market opportunities...")

    async def stop(self) -> None:
        """Gracefully terminates all network connections and tasks."""
        self.logger.log_warning("Initiating graceful engine shutdown...")
        self._running = False

        if self.ws_listener:
            await self.ws_listener.stop()

        if self._pumpportal_ws_task and not self._pumpportal_ws_task.done():
            self._pumpportal_ws_task.cancel()

        if self._dex_market_scanner_task and not self._dex_market_scanner_task.done():
            self._dex_market_scanner_task.cancel()

        if self._position_monitor_task and not self._position_monitor_task.done():
            self._position_monitor_task.cancel()

        if self._cluster_monitor_task and not self._cluster_monitor_task.done():
            self._cluster_monitor_task.cancel()

        await self.jupiter_client.close()
        await self.jito_client.close()
        await self.rpc_balancer.close()
        self.logger.log_success("Trading Engine shutdown complete.")

    async def handle_pool_detection(self, event: PoolDetectionEvent) -> None:
        """
        Processes newly detected pools, executing multi-tier security audits
        and triggering 5th-second sniper entry for approved setups.
        """
        # Strict Sequential Single-Position Mode: Only 1 active token at a time
        if len(self.active_positions) >= self.config.MAX_ACTIVE_POSITIONS:
            return

        # For live trading, require a valid Base58 token mint address
        if not self.config.DRY_RUN:
            if not event.token_mint:
                return
            try:
                from solders.pubkey import Pubkey
                Pubkey.from_string(event.token_mint)
            except Exception:
                return
            token_mint = event.token_mint
            pool_address = event.pool_address or event.token_mint
        else:
            token_mint = event.token_mint or f"MockToken{event.signature[:6]}"
            pool_address = event.pool_address or f"Pool{event.signature[:8]}"

        # Skip already monitored or active tokens
        if token_mint in self.active_positions or token_mint in self.monitored_pools:
            return

        # ---------------------------------------------------------------------
        # 5th-Second Sniper Timing: Wait until pool is exactly 5.0 seconds old
        # Bypasses block 0/1 MEV sandwich bundles and anti-bot traps
        # ---------------------------------------------------------------------
        now = time.time()
        detected_at = getattr(event, "detected_at", now)
        elapsed = now - detected_at
        remaining_delay = max(0.0, self.config.SNIPER_DELAY_SECONDS - elapsed)
        if remaining_delay > 0:
            await asyncio.sleep(remaining_delay)

        # Re-check dual-position guard after delay
        if len(self.active_positions) + self._pending_buys >= self.config.MAX_ACTIVE_POSITIONS:
            return

        # ---------------------------------------------------------------------
        # Verify 15+ Unique Traders / Transactions (Requirement: 15 traders)
        # ---------------------------------------------------------------------
        traders_count = 0
        try:
            import aiohttp
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2.5)) as session:
                async with session.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
                    headers={"User-Agent": "Mozilla/5.0"}
                ) as r:
                    if r.status == 200:
                        d = await r.json()
                        prs = d.get("pairs") or []
                        if prs:
                            txns = prs[0].get("txns", {})
                            traders_count = int(txns.get("m5", {}).get("buys", 0) + txns.get("m5", {}).get("sells", 0))
        except Exception:
            pass

        if traders_count < self.config.MIN_UNIQUE_BUYERS_3M:
            try:
                sigs = await self.rpc_balancer.call("getSignaturesForAddress", [token_mint, {"limit": 20}])
                if isinstance(sigs, list):
                    traders_count = max(traders_count, len(sigs))
            except Exception:
                pass

        if traders_count < self.config.MIN_UNIQUE_BUYERS_3M:
            self.logger.log_info(
                f"[bold yellow][WAITING 15 TRADERS][/] Token: [bold cyan]{token_mint[:8]}[/] "
                f"only has {traders_count} traders (< {self.config.MIN_UNIQUE_BUYERS_3M} required). Skipping entry."
            )
            return

        initial_sol = event.initial_sol_liquidity if event.initial_sol_liquidity > 0 else 35.0
        initial_tokens = 1_000_000_000.0 * 0.8  # 80% in pool

        # ---------------------------------------------------------------------
        # 1. Multi-Tier Anti-Scam & Liquidity Audit
        # ---------------------------------------------------------------------
        risk_score = await self.risk_evaluator.evaluate_token(
            token_mint=token_mint,
            pool_address=pool_address,
            sol_reserves=initial_sol,
            token_reserves=initial_tokens,
            skip_simulation=self.config.DRY_RUN,
        )

        self.logger.log_filter_audit(token_mint=token_mint, risk=risk_score)

        if not risk_score.passed_all_filters:
            return

        # ---------------------------------------------------------------------
        # 2. Register for Momentum and Volatility Tracking
        # ---------------------------------------------------------------------
        initial_price = initial_sol / initial_tokens
        self.volatility_engine.record_price(token_mint, initial_price)
        self.monitored_pools[token_mint] = {
            "pool_address": pool_address,
            "sol_reserves": initial_sol,
            "token_reserves": initial_tokens,
            "price_sol": initial_price,
            "risk_score": risk_score,
            "discovered_at": time.time(),
        }
        self.logger.monitored_tokens = self.monitored_pools

        # Trigger 5th-second sniper evaluation immediately
        await self.handle_trade_signal_evaluation(token_mint)

    async def handle_trade_signal_evaluation(self, token_mint: str) -> None:
        """
        Evaluates dynamic entry criteria: momentum velocity percentile,
        volatility regime, Kelly position sizing, and circuit breakers.
        """
        async with self._buy_lock:
            if len(self.active_positions) + self._pending_buys >= self.config.MAX_ACTIVE_POSITIONS:
                return
            self._pending_buys += 1

        try:
            pool_data = self.monitored_pools.get(token_mint)
            if not pool_data or token_mint in self.active_positions:
                return

            # 1. Check Circuit Breakers
            if self.circuit_breaker.is_drawdown_tripped:
                return

            # 2. Evaluate Momentum Inflow Velocity
            pool_age = time.time() - pool_data.get("discovered_at", 0)
            momentum = self.momentum_tracker.evaluate_momentum(token_mint)
            # For sniper listings (< 180s old), skip rolling 3-min momentum check to enable 5s sniper
            if pool_age > 180.0 and not momentum.is_90th_percentile:
                return

            # 3. Calculate Volatility Metrics
            volatility = self.volatility_engine.calculate_volatility(token_mint)

            # 4. Calculate Position Size ($5.00 entry per token)
            sol_price = 125.0
            target_sol = self.config.TARGET_BUY_USD / sol_price
            # Leave at least 0.005 SOL buffer for transaction fees
            max_allocatable = max(0.0, self.wallet_liquid_sol - 0.005)
            if max_allocatable < 0.01:
                self.logger.log_warning(
                    f"Insufficient balance ({self.wallet_liquid_sol:.4f} SOL) to allocate $5 trade. Keeping gas buffer."
                )
                return

            final_sol = min(target_sol, max_allocatable)

            self.logger.log_info(
                f"ENTRY TRIGGERED for [bold cyan]{token_mint[:8]}[/]! "
                f"Allocating: [bold white]{final_sol:.4f} SOL[/] (~${final_sol * sol_price:.2f} USD) | "
                f"Active Slots: {len(self.active_positions) + 1}/{self.config.MAX_ACTIVE_POSITIONS} | "
                f"Target: +{self.config.TARGET_TAKE_PROFIT_PCT:.0f}% Doubler ($5 -> $10)"
            )

            # 5. Execute Buy Swap via PumpPortal / Jupiter & Jito MEV
            success, sig, tokens_acquired = await self.executor.execute_buy(
                token_mint=token_mint,
                amount_sol=final_sol,
                slippage_bps=self.config.MAX_SLIPPAGE_BPS,
                is_congested=self.circuit_breaker.cluster_state.is_congested,
                is_exceptional_momentum=(momentum.buyer_growth_percentile >= 95.0),
            )

            if success and tokens_acquired > 0:
                # Obtain true entry market price from DexScreener or fill ratio
                entry_p = 0.0
                try:
                    import aiohttp
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3.0)) as session:
                        async with session.get(
                            f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
                            headers={"User-Agent": "Mozilla/5.0"}
                        ) as r:
                            if r.status == 200:
                                d = await r.json()
                                prs = d.get("pairs", [])
                                if prs and prs[0].get("priceNative"):
                                    entry_p = float(prs[0]["priceNative"])
                except Exception:
                    pass

                if entry_p <= 0 and tokens_acquired > 0:
                    entry_p = final_sol / float(tokens_acquired)
                if entry_p <= 0:
                    entry_p = pool_data.get("price_sol", 0.00000005)

                price_sol = entry_p
                initial_stop = price_sol * 0.65  # -35% hard stop baseline

                pos = OpenPosition(
                    token_mint=token_mint,
                    pool_address=pool_data["pool_address"],
                    pool_type=PoolType.PUMP_FUN if token_mint.endswith("pump") else PoolType.RAYDIUM_V4,
                    entry_price_sol=price_sol,
                    current_price_sol=price_sol,
                    peak_price_sol=price_sol,
                    tokens_amount=tokens_acquired,
                    sol_invested=final_sol,
                    entry_timestamp=time.time(),
                    trailing_stop_price=initial_stop,
                )
                self.active_positions[token_mint] = pos
                if not self.config.DRY_RUN:
                    self.wallet_liquid_sol = await self.rpc_balancer.get_balance(self.executor.pubkey_str)
                else:
                    self.wallet_liquid_sol -= final_sol
                self.logger.active_positions = self.active_positions
                self.logger.liquid_balance_sol = self.wallet_liquid_sol
        finally:
            async with self._buy_lock:
                self._pending_buys = max(0, self._pending_buys - 1)

    async def _position_monitoring_loop(self) -> None:
        """
        Continuously evaluates active positions for real-time DexScreener price ticks,
        +100% Take-Profit doubler ($5 -> $10), ATR trailing stop breaches, and stagnation cuts.
        """
        import aiohttp

        while self._running:
            try:
                await asyncio.sleep(1.0)
                if not self.active_positions:
                    continue

                for token_mint in list(self.active_positions.keys()):
                    pos = self.active_positions.get(token_mint)
                    if not pos or not pos.is_active:
                        continue

                    # 1. Fetch real-time market price via DexScreener
                    try:
                        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4.0)) as session:
                            async with session.get(
                                f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
                                headers={"User-Agent": "Mozilla/5.0"}
                            ) as resp:
                                if resp.status == 200:
                                    dex_data = await resp.json()
                                    pairs = dex_data.get("pairs", [])
                                    if pairs and pairs[0].get("priceNative"):
                                        live_p = float(pairs[0]["priceNative"])
                                        if live_p > 0:
                                            self.volatility_engine.record_price(token_mint, live_p)
                                            pos.current_price_sol = live_p
                    except Exception:
                        pass

                    # Update price tick
                    volatility = self.volatility_engine.calculate_volatility(token_mint)
                    momentum = self.momentum_tracker.evaluate_momentum(token_mint)

                    current_price = pos.current_price_sol
                    action, reason, fraction = self.exit_engine.evaluate_position_exit(
                        position=pos,
                        current_price_sol=current_price,
                        volatility=volatility,
                        momentum=momentum,
                    )

                    if action != TradeAction.HOLD:
                        tokens_to_sell = int(pos.tokens_amount * fraction)
                        success, sig, sol_back = await self.executor.execute_sell(
                            token_mint=token_mint,
                            tokens_amount=tokens_to_sell,
                            reason=action.value,
                            slippage_bps=self.config.MAX_SLIPPAGE_BPS,
                            is_congested=self.circuit_breaker.cluster_state.is_congested,
                        )

                        if success:
                            pos.tokens_amount -= tokens_to_sell
                            if not self.config.DRY_RUN:
                                self.wallet_liquid_sol = await self.rpc_balancer.get_balance(self.executor.pubkey_str)
                            else:
                                self.wallet_liquid_sol += sol_back
                            pos.realized_pnl_sol += sol_back - (pos.sol_invested * fraction)

                            if pos.tokens_amount <= 0 or fraction >= 0.99:
                                pos.is_active = False
                                del self.active_positions[token_mint]
                                if action == TradeAction.TAKE_PROFIT:
                                    self.logger.log_success(
                                        f"🚀 [bold green]100% PROFIT TARGET REALIZED (2x DOUBLED)![/] "
                                        f"Token: [bold cyan]{token_mint[:8]}[/] | Returned: [bold white]{sol_back:.4f} SOL[/] "
                                        f"(Net Profit: [bold green]+{pos.realized_pnl_sol:+.4f} SOL[/]). "
                                        f"Compounded capital added to wallet balance ({self.wallet_liquid_sol:.4f} SOL). "
                                        f"Now scanning for the NEXT token to snipe at the 5th second!"
                                    )
                                else:
                                    self.logger.log_success(
                                        f"Position Closed for [bold cyan]{token_mint[:8]}[/]! "
                                        f"Total Realized PnL: {pos.realized_pnl_sol:+.3f} SOL. Reason: {reason}"
                                    )

                # Update portfolio equity & check drawdown governor
                total_unrealized_sol = sum(p.unrealized_pnl_sol for p in self.active_positions.values())
                is_tripped, dd_pct = self.circuit_breaker.update_portfolio_equity(
                    current_liquid_sol=self.wallet_liquid_sol,
                    unrealized_pnl_sol=total_unrealized_sol,
                )
                self.logger.current_drawdown_pct = dd_pct
                self.logger.peak_equity_sol = self.circuit_breaker.peak_equity_sol
                self.logger.liquid_balance_sol = self.wallet_liquid_sol

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.log_error(f"Error in position monitoring loop: {e}")

    async def _cluster_telemetry_loop(self) -> None:
        """Periodically queries Solana cluster health every 20 seconds."""
        while self._running:
            try:
                state = await self.circuit_breaker.update_cluster_health()
                self.logger.cluster_tps = state.tps
                self.logger.cluster_fail_rate_pct = state.tx_failure_rate * 100.0
                self.logger.is_cluster_congested = state.is_congested
                await asyncio.sleep(20.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.log_warning(f"Error in cluster telemetry loop: {e}")
                await asyncio.sleep(10.0)

    async def _dex_market_scanner_loop(self) -> None:
        """
        Continuously polls DexScreener token profiles and token boosts to detect newly launched Solana
        tokens with market caps within $3,000 - $15,000 USD.
        Supplements WebSockets to provide ultra-fast candidate discovery.
        """
        import aiohttp

        seen_mints = set()
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

        while self._running:
            try:
                await asyncio.sleep(1.0)
                if len(self.active_positions) + self._pending_buys >= self.config.MAX_ACTIVE_POSITIONS:
                    continue

                candidate_tokens = []
                async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as session:
                    # 1. Fetch latest profiles
                    try:
                        async with session.get("https://api.dexscreener.com/token-profiles/latest/v1") as resp:
                            if resp.status == 200:
                                profiles = await resp.json()
                                if isinstance(profiles, list):
                                    candidate_tokens.extend([p.get("tokenAddress") for p in profiles if p.get("chainId") == "solana"])
                    except Exception:
                        pass

                    # 2. Fetch latest & top boosts
                    try:
                        async with session.get("https://api.dexscreener.com/token-boosts/top/v1") as resp:
                            if resp.status == 200:
                                top_b = await resp.json()
                                if isinstance(top_b, list):
                                    candidate_tokens.extend([b.get("tokenAddress") for b in top_b if b.get("chainId") == "solana"])
                    except Exception:
                        pass

                    try:
                        async with session.get("https://api.dexscreener.com/token-boosts/latest/v1") as resp:
                            if resp.status == 200:
                                boosts = await resp.json()
                                if isinstance(boosts, list):
                                    candidate_tokens.extend([b.get("tokenAddress") for b in boosts if b.get("chainId") == "solana"])
                    except Exception:
                        pass

                    for token_mint in candidate_tokens:
                        if not token_mint or token_mint in seen_mints or token_mint in self.active_positions or token_mint in self.monitored_pools:
                            continue
                        seen_mints.add(token_mint)

                        try:
                            token_api_url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
                            async with session.get(token_api_url) as pair_resp:
                                if pair_resp.status == 200:
                                    pair_data = await pair_resp.json()
                                    pairs = pair_data.get("pairs") or []
                                    p = pairs[0] if pairs else {}
                                    name = p.get("baseToken", {}).get("name", "Strong Gem")
                                    pair_addr = p.get("pairAddress") or token_mint
                                    mc = float(p.get("marketCap") or p.get("fdv") or 0)

                                    # Reject weak micro-caps - only allow strong tokens ($25k - $500k MC)
                                    if mc < self.config.MIN_MARKET_CAP_USD or mc > self.config.MAX_MARKET_CAP_USD:
                                        continue

                                    # Check minimum 15 traders
                                    txns = p.get("txns", {})
                                    traders = int(txns.get("m5", {}).get("buys", 0) + txns.get("m5", {}).get("sells", 0))
                                    if traders < self.config.MIN_UNIQUE_BUYERS_3M:
                                        continue

                                    liq_usd = float(p.get("liquidity", {}).get("usd", 0) or 0)
                                    if liq_usd < 3000.0:
                                        continue

                                    liq_sol = liq_usd / 125.0 if liq_usd > 0 else 24.0

                                    pool_type = PoolType.PUMP_FUN if token_mint.endswith("pump") else PoolType.RAYDIUM_V4
                                    event = PoolDetectionEvent(
                                        pool_address=pair_addr,
                                        token_mint=token_mint,
                                        base_mint=WSOL_MINT,
                                        quote_mint="",
                                        pool_type=pool_type,
                                        initial_sol_liquidity=liq_sol,
                                        signature=pair_addr[:16],
                                        detected_at=time.time(),
                                    )
                                    self.logger.log_info(
                                        f"[bold green][STRONG HIGH-CAP TOKEN][/] {name} "
                                        f"({token_mint[:8]}) | MC: ${mc:,.0f} | Liq: ${liq_usd:,.0f} | Dex: {p.get('dexId', 'raydium')}"
                                    )
                                    asyncio.create_task(self.handle_pool_detection(event))
                        except Exception as e:
                            self.logger.log_debug(f"Token lookup skipped for {token_mint}: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.log_debug(f"DexScreener scanner loop notice: {e}")
                await asyncio.sleep(2.0)

    async def _pumpportal_ws_stream_loop(self) -> None:
        """
        Connects directly to PumpPortal real-time WebSocket to receive newly listed Pump.fun tokens
        within milliseconds of creation on Solana ($3k - $15k MC).
        """
        import json
        import websockets

        uri = "wss://pumpportal.fun/api/data"
        while self._running:
            try:
                async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"method": "subscribeNewToken"}))
                    self.logger.log_success("Connected to PumpPortal Real-Time New Token Stream!")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(raw)
                            mint = data.get("mint")
                            if not mint:
                                continue
                            mc_sol = float(data.get("marketCapSol") or 28.0)
                            mc_usd = mc_sol * 125.0
                            if mc_usd < self.config.MIN_MARKET_CAP_USD or mc_usd > self.config.MAX_MARKET_CAP_USD:
                                continue

                            name = data.get("name", "PumpGem")
                            symbol = data.get("symbol", "PUMP")
                            self.logger.log_info(
                                f"[bold cyan][PUMP.FUN LIVE LISTING][/] New Token: {name} ({symbol}) | "
                                f"Mint: {mint[:8]}... | MC: ${mc_usd:,.0f} ({mc_sol:.1f} SOL)"
                            )

                            event = PoolDetectionEvent(
                                pool_address=mint,
                                token_mint=mint,
                                base_mint=WSOL_MINT,
                                quote_mint="",
                                pool_type=PoolType.PUMP_FUN,
                                initial_sol_liquidity=mc_sol,
                                signature=mint[:16],
                                detected_at=time.time(),
                            )
                            asyncio.create_task(self.handle_pool_detection(event))
                        except Exception:
                            continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.log_debug(f"PumpPortal WS reconnecting: {e}")
                await asyncio.sleep(2.0)

