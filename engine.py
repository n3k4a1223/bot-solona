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
        self._running = False
        self._position_monitor_task: Optional[asyncio.Task] = None
        self._cluster_monitor_task: Optional[asyncio.Task] = None

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

        self.logger.liquid_balance_sol = self.wallet_liquid_sol
        self.logger.peak_equity_sol = self.wallet_liquid_sol

        # 3. Start background supervision loops
        self._cluster_monitor_task = asyncio.create_task(self._cluster_telemetry_loop())
        self._position_monitor_task = asyncio.create_task(self._position_monitoring_loop())

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
        and registering valid tokens for momentum tracking.
        """
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

        initial_sol = event.initial_sol_liquidity if event.initial_sol_liquidity > 0 else 35.0
        initial_tokens = 1_000_000_000.0 * 0.8  # 80% in pool

        # Skip already monitored or active tokens
        if token_mint in self.active_positions or token_mint in self.monitored_pools:
            return

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

    async def handle_trade_signal_evaluation(self, token_mint: str) -> None:
        """
        Evaluates dynamic entry criteria: momentum velocity percentile,
        volatility regime, Kelly position sizing, and circuit breakers.
        """
        pool_data = self.monitored_pools.get(token_mint)
        if not pool_data or token_mint in self.active_positions:
            return

        # 1. Check Circuit Breakers
        if self.circuit_breaker.is_drawdown_tripped:
            return

        # 2. Evaluate Momentum Inflow Velocity
        momentum = self.momentum_tracker.evaluate_momentum(token_mint)
        if not momentum.is_90th_percentile:
            return

        # 3. Calculate Volatility Metrics
        volatility = self.volatility_engine.calculate_volatility(token_mint)

        # 4. Calculate Dynamic Position Size (Modified Kelly + Depth Adjusted)
        risk_score = pool_data["risk_score"]
        reserves_sol = pool_data["sol_reserves"]
        size_rec = self.position_sizer.calculate_size(
            wallet_balance_sol=self.wallet_liquid_sol,
            pool_sol_reserves=reserves_sol,
            risk_score=risk_score,
            volatility=volatility,
            momentum=momentum,
        )

        if size_rec.allocated_sol < 0.008:
            return

        # Congestion throttle adjustment
        final_sol = size_rec.allocated_sol * self.circuit_breaker.cluster_state.throttle_multiplier

        self.logger.log_info(
            f"ENTRY TRIGGERED for [bold cyan]{token_mint[:8]}[/]! "
            f"Allocating: [bold white]{final_sol:.3f} SOL[/] ({size_rec.final_allocation_pct*100:.1f}%) | "
            f"{size_rec.rationale}"
        )

        # 5. Execute Buy Swap via Jupiter & Jito MEV
        success, sig, tokens_acquired = await self.executor.execute_buy(
            token_mint=token_mint,
            amount_sol=final_sol,
            slippage_bps=self.config.MAX_SLIPPAGE_BPS,
            is_congested=self.circuit_breaker.cluster_state.is_congested,
            is_exceptional_momentum=(momentum.buyer_growth_percentile >= 95.0),
        )

        if success and tokens_acquired > 0:
            price_sol = pool_data["price_sol"]
            initial_stop = price_sol - (self.config.ATR_MULTIPLIER * volatility.atr_sol)

            pos = OpenPosition(
                token_mint=token_mint,
                pool_address=pool_data["pool_address"],
                pool_type=PoolType.RAYDIUM_V4,
                entry_price_sol=price_sol,
                current_price_sol=price_sol,
                peak_price_sol=price_sol,
                tokens_amount=tokens_acquired,
                sol_invested=final_sol,
                entry_timestamp=time.time(),
                trailing_stop_price=initial_stop,
            )
            self.active_positions[token_mint] = pos
            self.wallet_liquid_sol -= final_sol
            self.logger.active_positions = self.active_positions
            self.logger.liquid_balance_sol = self.wallet_liquid_sol

    async def _position_monitoring_loop(self) -> None:
        """
        Continuously evaluates active positions for ATR trailing stop breaches,
        buyer volume exhaustion scale-outs, and stagnation cuts.
        """
        while self._running:
            try:
                await asyncio.sleep(1.5)
                if not self.active_positions:
                    continue

                for token_mint in list(self.active_positions.keys()):
                    pos = self.active_positions.get(token_mint)
                    if not pos or not pos.is_active:
                        continue

                    # Update price tick
                    volatility = self.volatility_engine.calculate_volatility(token_mint)
                    momentum = self.momentum_tracker.evaluate_momentum(token_mint)

                    current_price = volatility.current_price_sol
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
                            self.wallet_liquid_sol += sol_back
                            pos.realized_pnl_sol += sol_back - (pos.sol_invested * fraction)

                            if pos.tokens_amount <= 0 or fraction >= 0.99:
                                pos.is_active = False
                                del self.active_positions[token_mint]
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
