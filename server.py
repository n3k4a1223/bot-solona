"""
Production Web Server for Render Hosting
=========================================
Asynchronous HTTP server powered by aiohttp to serve the Solana Quantitative
Trading Terminal and health check endpoints on Render (or any cloud host).
"""

import os
import sys
import time
from aiohttp import web

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 8080))

async def handle_index(request: web.Request) -> web.Response:
    """Serve the primary terminal dashboard."""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    if not os.path.exists(html_path):
        html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    return web.Response(text=content, content_type="text/html")


async def handle_health(request: web.Request) -> web.Response:
    """Render health check endpoint."""
    return web.json_response({
        "status": "healthy",
        "service": "solana-trading-bot",
        "regime": "micro-capital-compounder",
        "wallet": "7zAUSFzM6KAZJ8x7QGqtMVT3K3JxYK2zATcUxyChs2H3",
        "timestamp": time.time(),
    })


async def handle_api_status(request: web.Request) -> web.Response:
    """Telemetry API endpoint."""
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
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/status", handle_api_status)
    return app


if __name__ == "__main__":
    print(f"[*] Starting Solana Dashboard Web Server on {HOST}:{PORT}")
    app = create_app()
    web.run_app(app, host=HOST, port=PORT)
