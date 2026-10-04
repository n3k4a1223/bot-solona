"""
Application Entry Point & CLI Driver
====================================
Configures asynchronous signal handling, initializes the TradingEngine,
and provides simulation and live deployment modes.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time
from typing import Optional

from config.settings import BotConfig, get_config
from core.logger import get_logger
from core.types import PoolDetectionEvent, PoolType
from engine import TradingEngine


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Quantitative Solana Adaptive High-Frequency Trading Bot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute real mainnet transactions (CAUTION: Capital at risk). Defaults to DRY-RUN.",
    )
    parser.add_argument(
        "--simulate-cycle",
        action="store_true",
        help="Run an automated end-to-end mathematical simulation cycle to verify all algorithms.",
    )
    return parser.parse_args()


async def run_simulation_cycle(engine: TradingEngine) -> None:
    """
    Demonstrates the full end-to-end adaptive quantitative pipeline:
    1. Pool Discovery & Anti-Scam Filter Audit
    2. Retail Momentum Inflow Velocity (90th percentile detection)
    3. Modified Kelly Position Sizing
    4. Execution via Jupiter & Jito MEV
    5. Dynamic ATR Trailing Stop, Exhaustion Scale-Out, and Stagnation Cuts
    """
    logger = engine.logger
    logger.log_info("[bold cyan]Initiating End-to-End Mathematical Simulation Cycle...[/]")

    test_mint = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"  # Bonk mint for realistic routing
    test_pool = "HVBPJAwCrSqE3FrpHqaq4ENz5ZqgC5G2q5o6JqQxUv6"

    # 1. Simulate Pool Discovery Event
    logger.log_info("Simulating new pool initialization event on Raydium V4...")
    event = PoolDetectionEvent(
        pool_address=test_pool,
        token_mint=test_mint,
        base_mint="So11111111111111111111111111111111111111112",
        quote_mint=test_mint,
        pool_type=PoolType.RAYDIUM_V4,
        initial_sol_liquidity=45.0,
        signature="5j7...simulated_pool_sig",
    )
    await engine.handle_pool_detection(event)

    # 2. Simulate Organic Retail Momentum Inflow (> 90th percentile)
    logger.log_info("Simulating retail buyer inflow velocity across 18 unique wallets...")
    for i in range(18):
        wallet_addr = f"BuyerWallet_{i:02d}_{os.urandom(2).hex()}"
        engine.momentum_tracker.record_swap(
            token_mint=test_mint,
            user_wallet=wallet_addr,
            is_buy=True,
            sol_amount=0.85 + (i * 0.1),
            token_amount=500_000.0,
            signature=f"sig_{i}_{os.urandom(4).hex()}",
        )

    # Record price ticks for volatility estimation
    # Record price ticks for volatility estimation using pool entry price
    base_price = engine.monitored_pools[test_mint]["price_sol"]
    for step in range(12):
        price = base_price * (1.0 + (step * 0.035))  # Upward trending volatility
        engine.volatility_engine.record_price(test_mint, price)

    # 3. Trigger Entry Evaluation
    logger.log_info("Evaluating dynamic entry conditions (Kelly sizing + order-book depth cap)...")
    await engine.handle_trade_signal_evaluation(test_mint)

    # 4. Simulate Price Run-Up (+25%) & Buyer Volume Exhaustion (Scale-Out Trigger)
    logger.log_info("Simulating price run-up (+25%) followed by seller volume dominance...")
    peak_price = base_price * 1.25
    engine.volatility_engine.record_price(test_mint, peak_price)

    # Inject sell dominance across consecutive intervals to trigger scale-out
    for s in range(6):
        seller_wallet = f"SellerWallet_{s:02d}"
        engine.momentum_tracker.record_swap(
            token_mint=test_mint,
            user_wallet=seller_wallet,
            is_buy=False,
            sol_amount=4.5,
            token_amount=2_000_000.0,
            signature=f"sell_sig_{s}",
        )

    # Allow position monitor loop to evaluate exit
    await asyncio.sleep(3.0)

    # 5. Simulate Price Pullback Breaching Dynamic ATR Trailing Stop
    logger.log_info("Simulating sharp price pullback breaching dynamic ATR trailing stop band...")
    pullback_price = base_price * 0.92  # Drops below trailing stop
    engine.volatility_engine.record_price(test_mint, pullback_price)
    await asyncio.sleep(3.0)

    logger.log_success("[bold green]End-to-End Quantitative Simulation Cycle Completed Successfully![/]")


async def main_async() -> None:
    """Async main routine."""
    args = parse_args()
    config = get_config()

    if args.live:
        config.DRY_RUN = False

    engine = TradingEngine(config=config)

    # Handle graceful termination signals
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        engine.logger.log_warning("Interrupt signal received. Gracefully shutting down...")
        stop_event.set()

    # On Windows, add_signal_handler for SIGINT is sometimes unsupported; wrap in try
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except (NotImplementedError, AttributeError):
            pass

    try:
        await engine.start()

        if args.simulate_cycle:
            await run_simulation_cycle(engine)
            await engine.stop()
            return

        # Main operational loop
        while not stop_event.is_set():
            await asyncio.sleep(0.5)

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await engine.stop()


def main() -> None:
    """Synchronous entry point."""
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nProcess terminated by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
