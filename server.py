"""
Production Web Server for Render / Cloud Hosting
================================================
Asynchronous HTTP server powered by aiohttp to serve both:
1. Solana Quantitative Trading Terminal (index.html / dashboard.html)
2. Standalone OKX Quantitative Terminal (okx.html)
Provides live REST API telemetry, health checks, and background quantitative scan cycles.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

from execution.okx_client import OKXClient
from okx_engine import OKXQuantitativeEngine

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))

# Global OKX Engine instance
okx_client_instance: OKXClient | None = None
okx_engine_instance: OKXQuantitativeEngine | None = None


def get_okx_instances():
    global okx_client_instance, okx_engine_instance
    if okx_engine_instance is None:
        api_key = os.getenv("OKX_API_KEY", "")
        secret_key = os.getenv("OKX_SECRET_KEY", "")
        passphrase = os.getenv("OKX_PASSPHRASE", "")
        simulated = os.getenv("OKX_SIMULATED", "true").lower() in ("true", "1", "yes")

        if api_key and secret_key and passphrase:
            okx_client_instance = OKXClient(
                api_key=api_key,
                secret_key=secret_key,
                passphrase=passphrase,
                simulated=simulated,
                rest_url=os.getenv("OKX_REST_URL", "https://www.okx.com"),
            )
            okx_engine_instance = OKXQuantitativeEngine(okx_client=okx_client_instance)
    return okx_client_instance, okx_engine_instance


async def okx_background_worker(app: web.Application):
    """Background task running periodic OKX quantitative analysis and stop management."""
    _, engine = get_okx_instances()
    if not engine:
        return

    print("[*] OKX background quantitative worker started.")
    try:
        while True:
            try:
                await engine.run_cycle()
            except Exception as e:
                print(f"[!] OKX worker error: {e}")
            await asyncio.sleep(3)
    except asyncio.CancelledError:
        print("[*] OKX background worker stopped.")


async def start_background_tasks(app: web.Application):
    app["okx_worker"] = asyncio.create_task(okx_background_worker(app))


async def cleanup_background_tasks(app: web.Application):
    if "okx_worker" in app:
        app["okx_worker"].cancel()
        await app["okx_worker"]
    global okx_client_instance
    if okx_client_instance:
        await okx_client_instance.close()


async def handle_index(request: web.Request) -> web.Response:
    """Serve the primary Solana terminal dashboard."""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(html_path):
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html")


async def handle_okx(request: web.Request) -> web.Response:
    """Serve the dedicated standalone OKX terminal dashboard."""
    html_path = os.path.join(os.path.dirname(__file__), "okx.html")
    if not os.path.exists(html_path):
        return web.Response(text="OKX dashboard template not found", status=404)

    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html")


async def handle_okx_status(request: web.Request) -> web.Response:
    """Return real-time OKX quantitative strategy telemetry."""
    _, engine = get_okx_instances()
    if engine:
        return web.json_response(engine.get_telemetry())
    return web.json_response({
        "status": "unconfigured",
        "msg": "OKX credentials not set in environment",
        "market_indicators": [],
        "open_positions": [],
        "logs": [],
    })


async def handle_okx_balance(request: web.Request) -> web.Response:
    """Return fresh account balance directly from OKX."""
    client, _ = get_okx_instances()
    if client:
        bal = await client.get_balance()
        return web.json_response(bal)
    return web.json_response({"success": False, "msg": "OKX client unconfigured"}, status=400)


async def handle_health(request: web.Request) -> web.Response:
    """Render health check endpoint."""
    return web.json_response({
        "status": "healthy",
        "service": "unified-trading-server",
        "solana_regime": "micro-capital-compounder",
        "okx_regime": "atr-ratchet-momentum",
        "timestamp": time.time(),
    })


async def handle_api_status(request: web.Request) -> web.Response:
    """Solana telemetry API endpoint."""
    return web.json_response({
        "status": "active",
        "network": "solana-mainnet",
        "strategy": {
            "mode": "Micro-Capital Aggressive Scalp",
            "reserve_buffer_sol": 0.005,
            "min_inflow_percentile": 90.0,
            "exit_engine": "2.0x ATR Trailing Stop + 50% Exhaustion Lock",
        },
        "target_wallet": "7zAUSFzM6KAZJ8x7QGqtMVT3K3JxYK2zATcUxyChs2H3",
        "solscan_url": "https://solscan.io/account/7zAUSFzM6KAZJ8x7QGqtMVT3K3JxYK2zATcUxyChs2H3",
    })


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/dashboard", handle_index)
    app.router.add_get("/okx", handle_okx)
    app.router.add_get("/okx.html", handle_okx)
    app.router.add_get("/api/okx/status", handle_okx_status)
    app.router.add_get("/api/okx/balance", handle_okx_balance)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/status", handle_api_status)

    app.on_startup.append(start_background_tasks)
    app.on_cleanup.append(cleanup_background_tasks)
    return app


if __name__ == "__main__":
    print(f"[*] Starting Unified Solana & OKX Trading Web Server on {HOST}:{PORT}")
    app = create_app()
    web.run_app(app, host=HOST, port=PORT)
