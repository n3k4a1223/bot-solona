"""
OKX Quantitative Trading Terminal - Main Execution Runner
=========================================================
Runs the autonomous OKX quantitative momentum & ATR trailing stop bot.
Usage:
    python okx_main.py                 (Defaults to SIMULATED/DEMO mode)
    python okx_main.py --simulated     (Explicit Simulated Demo Mode)
    python okx_main.py --live          (Live Mainnet Trading with real capital)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
import time

# Ensure immediate unbuffered UTF-8 output on Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

from dotenv import load_dotenv

load_dotenv()

from execution.okx_client import OKXClient
from okx_engine import OKXQuantitativeEngine


def parse_args():
    parser = argparse.ArgumentParser(description="OKX Quantitative Algorithmic Trading Bot")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run against OKX Live mainnet (WARNING: commits real capital)",
    )
    parser.add_argument(
        "--simulated",
        action="store_true",
        help="Run against OKX Simulated / Demo mode",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Cycle interval in seconds (default: 2.0s for high-speed scalping)",
    )
    return parser.parse_args()


async def main():
    args = parse_args()
    
    # Determine simulation vs live mode
    simulated = True
    if args.live:
        simulated = False
    elif args.simulated:
        simulated = True
    else:
        env_sim = os.getenv("OKX_SIMULATED", "true").lower()
        simulated = env_sim in ("true", "1", "yes")

    api_key = os.getenv("OKX_API_KEY", "")
    secret_key = os.getenv("OKX_SECRET_KEY", "")
    passphrase = os.getenv("OKX_PASSPHRASE", "")
    rest_url = os.getenv("OKX_REST_URL", "https://www.okx.com")

    if not api_key or not secret_key or not passphrase:
        print("[!] ERROR: Missing OKX API credentials in .env file.", flush=True)
        print("    Please set OKX_API_KEY, OKX_SECRET_KEY, and OKX_PASSPHRASE.", flush=True)
        sys.exit(1)

    print("=" * 72, flush=True)
    print("      OKX QUANTITATIVE ALGORITHMIC TRADING TERMINAL", flush=True)
    print(f"      Requested Mode: {'DEMO / SIMULATED' if simulated else 'LIVE MAINNET'}", flush=True)
    print(f"      API Key: {api_key[:8]}...{api_key[-6:]}", flush=True)
    print("=" * 72, flush=True)

    client = OKXClient(
        api_key=api_key,
        secret_key=secret_key,
        passphrase=passphrase,
        simulated=simulated,
        rest_url=rest_url,
    )

    # Initial Balance Check
    bal = await client.get_balance()
    if not bal.get("success"):
        err_msg = str(bal.get("msg", ""))
        if "50101" in err_msg or "APIKey does not match current environment" in err_msg:
            print("\n" + "!" * 72, flush=True)
            print("[!] ئاگاداری گرنگ دەربارەی کلیلی API (OKX Environment Notice):", flush=True)
            print("    ئەم کلیلی APIەی پێمانت داوە (3f8086d3...) تایبەتە بە دۆخی ئەزموونی (Demo Trading) لەناو OKX.", flush=True)
            print("    لەبەر ئەوە OKX ڕێگە نادات لەسەر Live بەکاربێت (Code 50101).", flush=True)
            print("    This API Key was created inside OKX Demo/Simulated Trading mode.", flush=True)
            print("    OKX disallows calling Live endpoints with a Demo API key.", flush=True)
            print("!" * 72 + "\n", flush=True)

            if not simulated:
                print("[*] بە شێوەی ئۆتۆماتیکی دەگۆڕدرێت بۆ دۆخی Simulated تا ڕۆبۆتەکە بە سەرمایەی Demo کاربکات...", flush=True)
                print("[*] Automatically falling back to Simulated Demo mode ($98k demo capital)...", flush=True)
                client.simulated = True
                simulated = True
                bal = await client.get_balance()

    engine = OKXQuantitativeEngine(
        okx_client=client,
        atr_multiplier=2.0,
        volume_surge_threshold=1.8,
        max_daily_drawdown_pct=0.06,
    )

    if bal.get("success"):
        print(f"[*] Account Equity: ${bal.get('total_eq_usd', 0):,.2f} USD", flush=True)
        print("[*] Strategy Engine: OKX Ultra-Fast All-Coin Momentum Scalper (Scanning 400+ Coins)", flush=True)
        print("[*] Lightning-Fast Profit Locks: +0.5% Breakeven | +1.2% (50% Lock) | +2.2% (Tier 2 Lock)", flush=True)
        print(f"[*] Dynamic Trailing Multiplier: 1.2x ATR (Tightens to 0.8x in profit)", flush=True)
    else:
        print(f"[!] Warning: Initial balance fetch failed: {bal.get('msg')}", flush=True)

    print("\n[*] Starting autonomous high-speed scan loop. Press Ctrl+C to exit.\n", flush=True)

    try:
        while True:
            await engine.run_cycle()
            t = engine.get_telemetry()
            
            # Print high-frequency heartbeat
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Equity: ${t['total_equity_usd']:,.2f} | "
                f"Cash: ${t['cash_usdt']:,.2f} | "
                f"Coins Tracked: {t.get('universe_coins_tracked', 0)}/{t.get('total_market_pairs', 0)} | "
                f"Open Positions: {len(t['open_positions'])} | "
                f"Drawdown: {t['current_drawdown_pct']:.2f}% "
                f"{'[CIRCUIT TRIPPED]' if t['circuit_tripped'] else ''}",
                flush=True
            )
            
            # Log any active positions
            for pos in t["open_positions"]:
                pnl_color = "+" if pos['pnl_pct'] >= 0 else ""
                print(
                    f"    ⚡ [{pos['inst_id']}] Sz: {pos['size']} | Entry: ${pos['entry_price']} | "
                    f"Stop: ${pos['trailing_stop']} | PnL: {pnl_color}{pos['pnl_pct']:.2f}% (${pnl_color}{pos['pnl_usd']:.2f})"
                    f"{' [🛡️ BREAKEVEN]' if pos.get('breakeven_set') else ''}"
                    f"{' [⚡ 50% PROFIT LOCKED]' if pos.get('scaled_out_tier1') else ''}"
                    f"{' [🚀 TIER 2 LOCKED]' if pos.get('scaled_out_tier2') else ''}",
                    flush=True
                )

            await asyncio.sleep(args.interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[*] Graceful shutdown initiated...", flush=True)
    finally:
        await client.close()
        print("[*] OKX client session closed. Engine stopped.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
