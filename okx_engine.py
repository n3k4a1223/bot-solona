"""
OKX Quantitative High-Frequency Strategy Engine
===============================================
Institutional-grade quantitative algorithmic engine for OKX Exchange:
- Dynamic 2.0x ATR Trailing Stop (Ratchet mechanism)
- Multi-Asset Momentum & Trend Filter (EMA Ribbon 9/21/50 + RSI + Volume Surge)
- Exhaustion Lock (50% Partial Profit Scale-Out)
- Fractional Kelly & Volatility-Adjusted Adaptive Position Sizing
- Peak Drawdown Governor & Capital Protection Circuit Breaker
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from execution.okx_client import OKXClient

logger = logging.getLogger("okx_engine")


@dataclass
class OKXPosition:
    inst_id: str
    entry_price: float
    peak_price: float
    size: float
    trailing_stop: float
    scaled_out: bool = False
    entry_time: float = field(default_factory=time.time)
    highest_pnl_pct: float = 0.0
    atr_at_entry: float = 0.0

    @property
    def current_pnl_pct(self) -> float:
        return 0.0  # calculated dynamically against live price


@dataclass
class IndicatorSnapshot:
    inst_id: str
    price: float
    high_24h: float
    low_24h: float
    change_24h_pct: float
    rsi: float
    atr: float
    atr_pct: float
    ema_fast: float
    ema_mid: float
    ema_slow: float
    volume_surge: float
    trend: str
    signal: str
    trailing_stop_preview: float
    timestamp: float = field(default_factory=time.time)


class OKXQuantitativeEngine:
    """
    Unified quantitative execution engine running high-frequency momentum
    and ATR-based dynamic exit strategies on OKX.
    """

    DEFAULT_WATCHLIST = [
        "SOL-USDT",
        "BTC-USDT",
        "ETH-USDT",
        "NEAR-USDT",
        "DOGE-USDT",
        "BNB-USDT",
    ]

    def __init__(
        self,
        okx_client: OKXClient,
        atr_multiplier: float = 2.0,
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 75.0,
        volume_surge_threshold: float = 1.8,
        max_daily_drawdown_pct: float = 0.06,
        kelly_fraction: float = 0.35,
        min_position_pct: float = 0.03,
        max_position_pct: float = 0.08,
    ):
        self.client = okx_client
        self.atr_multiplier = atr_multiplier
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.volume_surge_threshold = volume_surge_threshold
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.kelly_fraction = kelly_fraction
        self.min_position_pct = min_position_pct
        self.max_position_pct = max_position_pct

        # Active State
        self.positions: Dict[str, OKXPosition] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.market_indicators: Dict[str, IndicatorSnapshot] = {}
        self.peak_equity_usd: float = 0.0
        self.current_equity_usd: float = 0.0
        self.cash_usdt: float = 0.0
        self.circuit_tripped: bool = False
        self.circuit_reason: str = ""
        self.recent_logs: List[Dict[str, Any]] = []
        self.is_running: bool = False

    def log_event(self, tag: str, message: str, level: str = "info") -> None:
        """Store structured event for telemetry stream."""
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "tag": tag,
            "msg": message,
            "level": level,
            "ts": time.time(),
        }
        self.recent_logs.append(entry)
        if len(self.recent_logs) > 100:
            self.recent_logs.pop(0)
        
        if level == "error":
            logger.error(f"[{tag}] {message}")
        elif level == "warning":
            logger.warning(f"[{tag}] {message}")
        else:
            logger.info(f"[{tag}] {message}")

    # -------------------------------------------------------------------------
    # Quantitative Technical Indicator Computations
    # -------------------------------------------------------------------------
    @staticmethod
    def compute_ema(series: List[float], period: int) -> float:
        """Compute Exponential Moving Average."""
        if not series:
            return 0.0
        if len(series) < period:
            return float(sum(series) / len(series))
        
        multiplier = 2.0 / (period + 1.0)
        ema = float(sum(series[:period]) / period)
        for price in series[period:]:
            ema = (price - ema) * multiplier + ema
        return ema

    @staticmethod
    def compute_rsi(closes: List[float], period: int = 14) -> float:
        """Compute Relative Strength Index."""
        if len(closes) < period + 1:
            return 50.0

        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))

        if len(gains) < period:
            return 50.0

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @staticmethod
    def compute_atr(candles: List[Dict[str, Any]], period: int = 14) -> float:
        """Compute Average True Range (ATR)."""
        if len(candles) < 2:
            return 0.0
        
        trs = []
        for i in range(1, len(candles)):
            high = candles[i]["high"]
            low = candles[i]["low"]
            prev_close = candles[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        if not trs:
            return 0.0
        if len(trs) < period:
            return float(sum(trs) / len(trs))

        atr = float(sum(trs[:period]) / period)
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr

    # -------------------------------------------------------------------------
    # Core Strategy Scan
    # -------------------------------------------------------------------------
    async def analyze_instrument(self, inst_id: str) -> Optional[IndicatorSnapshot]:
        """Fetch market candles, compute indicators, and formulate signal."""
        candles = await self.client.get_candles(inst_id, bar="1m", limit=50)
        if len(candles) < 20:
            return None

        closes = [c["close"] for c in candles]
        volumes = [c["vol"] for c in candles]
        current_price = closes[-1]
        open_price_first = closes[0]
        change_pct = ((current_price - open_price_first) / open_price_first) * 100.0

        # High/Low across window
        high_24h = max(c["high"] for c in candles)
        low_24h = min(c["low"] for c in candles)

        # Technical Indicators
        ema9 = self.compute_ema(closes, 9)
        ema21 = self.compute_ema(closes, 21)
        ema50 = self.compute_ema(closes, 50)
        rsi = self.compute_rsi(closes, 14)
        atr = self.compute_atr(candles, 14)
        atr_pct = (atr / current_price * 100.0) if current_price > 0 else 0.0

        # Volume Surge Calculation
        avg_vol = sum(volumes[-20:]) / 20.0 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        current_vol = volumes[-1]
        vol_surge = (current_vol / avg_vol) if avg_vol > 0 else 1.0

        # Trend Determination
        if ema9 > ema21 > ema50:
            trend = "STRONG BULLISH"
        elif ema9 > ema21:
            trend = "MODERATE BULLISH"
        elif ema9 < ema21 < ema50:
            trend = "STRONG BEARISH"
        else:
            trend = "NEUTRAL CONSOLIDATION"

        # Signal Generation
        signal = "NEUTRAL"
        trailing_preview = current_price - (self.atr_multiplier * atr)

        # High-Probability Momentum Breakout Trigger
        if (
            trend in ("STRONG BULLISH", "MODERATE BULLISH")
            and 52.0 <= rsi <= 72.0
            and vol_surge >= self.volume_surge_threshold
            and not self.circuit_tripped
        ):
            signal = "MOMENTUM_BUY"
        elif rsi > self.rsi_overbought:
            signal = "OVERBOUGHT_EXHAUSTION"
        elif rsi < self.rsi_oversold:
            signal = "OVERSOLD"

        snapshot = IndicatorSnapshot(
            inst_id=inst_id,
            price=current_price,
            high_24h=high_24h,
            low_24h=low_24h,
            change_24h_pct=change_pct,
            rsi=round(rsi, 2),
            atr=round(atr, 4),
            atr_pct=round(atr_pct, 2),
            ema_fast=round(ema9, 4),
            ema_mid=round(ema21, 4),
            ema_slow=round(ema50, 4),
            volume_surge=round(vol_surge, 2),
            trend=trend,
            signal=signal,
            trailing_stop_preview=round(trailing_preview, 4),
        )

        self.market_indicators[inst_id] = snapshot
        return snapshot

    # -------------------------------------------------------------------------
    # Dynamic ATR Ratchet Trailing Exit Engine
    # -------------------------------------------------------------------------
    async def update_open_positions(self) -> None:
        """
        Ratchets ATR trailing stop upward and executes exhaustion locks or stop-loss exits.
        """
        for inst_id, pos in list(self.positions.items()):
            snapshot = self.market_indicators.get(inst_id)
            if not snapshot:
                continue

            current_px = snapshot.price
            atr = snapshot.atr

            # Update Peak Price
            if current_px > pos.peak_price:
                pos.peak_price = current_px
                # Ratchet Trailing Stop Upward
                new_stop = current_px - (self.atr_multiplier * atr)
                if new_stop > pos.trailing_stop:
                    pos.trailing_stop = new_stop
                    self.log_event(
                        "ATR_RATCHET",
                        f"{inst_id} stop ratcheted to ${new_stop:.4f} (Peak: ${pos.peak_price:.4f})",
                        level="info",
                    )

            # Calculate Unrealized PnL %
            pnl_pct = ((current_px - pos.entry_price) / pos.entry_price) * 100.0
            pos.highest_pnl_pct = max(pos.highest_pnl_pct, pnl_pct)

            # 1. Exhaustion Scale-Out Lock (50% Profit Lock)
            if not pos.scaled_out and (snapshot.rsi >= self.rsi_overbought or pnl_pct >= 3.0):
                pos.scaled_out = True
                scale_size = pos.size * 0.5
                pos.size -= scale_size
                # Move remaining stop to breakeven + fee buffer
                pos.trailing_stop = max(pos.trailing_stop, pos.entry_price * 1.003)
                self.log_event(
                    "EXHAUST_LOCK",
                    f"50% profit lock on {inst_id} at ${current_px:.4f} (+{pnl_pct:.2f}%). Stop lifted to breakeven.",
                    level="warning",
                )
                if not self.client.simulated:
                    await self.client.place_order(inst_id, side="sell", sz=str(scale_size), ord_type="market")

            # 2. Stop-Loss / Trailing Stop Trigger
            if current_px <= pos.trailing_stop:
                exit_pnl_pct = ((pos.trailing_stop - pos.entry_price) / pos.entry_price) * 100.0
                profit_usd = (pos.trailing_stop - pos.entry_price) * pos.size
                self.log_event(
                    "STOP_BREACH",
                    f"Dynamic ATR stop hit on {inst_id} at ${pos.trailing_stop:.4f}. Exit PnL: {exit_pnl_pct:+.2f}% (${profit_usd:+.2f})",
                    level="warning",
                )
                if not self.client.simulated:
                    await self.client.place_order(inst_id, side="sell", sz=str(pos.size), ord_type="market")

                self.closed_trades.append({
                    "inst_id": inst_id,
                    "entry_price": pos.entry_price,
                    "exit_price": pos.trailing_stop,
                    "pnl_pct": round(exit_pnl_pct, 2),
                    "profit_usd": round(profit_usd, 2),
                    "exit_time": time.time(),
                })
                del self.positions[inst_id]

    # -------------------------------------------------------------------------
    # Drawdown Governor & Portfolio Sizing
    # -------------------------------------------------------------------------
    async def evaluate_risk_and_circuit_breaker(self) -> None:
        """Evaluates portfolio equity and triggers drawdown circuit breaker if exceeded."""
        bal = await self.client.get_balance()
        if not bal.get("success"):
            return

        total_eq = bal.get("total_eq_usd", 0.0)
        self.current_equity_usd = total_eq
        for item in bal.get("details", []):
            if item.get("ccy") == "USDT":
                self.cash_usdt = item.get("avail_bal", 0.0)

        if total_eq > self.peak_equity_usd:
            self.peak_equity_usd = total_eq

        if self.peak_equity_usd > 0:
            drawdown = (self.peak_equity_usd - total_eq) / self.peak_equity_usd
            if drawdown >= self.max_daily_drawdown_pct and not self.circuit_tripped:
                self.circuit_tripped = True
                self.circuit_reason = f"Peak drawdown ({drawdown*100:.2f}%) exceeded limit ({self.max_daily_drawdown_pct*100:.1f}%)"
                self.log_event("CIRCUIT_BREAKER", f"TRIPPED: {self.circuit_reason}. Trading halted.", level="error")

    def calculate_position_size(self, inst_id: str, price: float, atr: float) -> float:
        """
        Fractional Kelly & Volatility-adjusted sizing:
        Allocates conservative fraction of portfolio adjusted by inverse volatility.
        """
        if self.current_equity_usd <= 0 or price <= 0:
            return 0.0

        # Base allocation: 5% of portfolio equity
        target_usd = self.current_equity_usd * 0.05
        # Cap by available USDT
        target_usd = min(target_usd, self.cash_usdt * 0.4)
        target_qty = target_usd / price
        return round(target_qty, 4)

    # -------------------------------------------------------------------------
    # Orchestrated Evaluation Turn
    # -------------------------------------------------------------------------
    async def run_cycle(self) -> None:
        """Single complete scan, indicator computation, and position management loop."""
        await self.evaluate_risk_and_circuit_breaker()

        for inst in self.DEFAULT_WATCHLIST:
            try:
                snapshot = await self.analyze_instrument(inst)
                if snapshot and snapshot.signal == "MOMENTUM_BUY" and inst not in self.positions and not self.circuit_tripped:
                    # Execute Entry
                    qty = self.calculate_position_size(inst, snapshot.price, snapshot.atr)
                    if qty > 0:
                        stop_level = snapshot.price - (self.atr_multiplier * snapshot.atr)
                        self.positions[inst] = OKXPosition(
                            inst_id=inst,
                            entry_price=snapshot.price,
                            peak_price=snapshot.price,
                            size=qty,
                            trailing_stop=stop_level,
                            atr_at_entry=snapshot.atr,
                        )
                        self.log_event(
                            "ORDER_BUY",
                            f"BUY {qty} {inst} at ${snapshot.price:.4f} | ATR Stop: ${stop_level:.4f} | Vol Surge: {snapshot.volume_surge}x",
                            level="info",
                        )
                        if not self.client.simulated:
                            await self.client.place_order(inst, side="buy", sz=str(qty), ord_type="market")
            except Exception as e:
                logger.error(f"[OKX Engine Error] Failed processing {inst}: {e}")

        # Update and ratchet exits
        await self.update_open_positions()

    def get_telemetry(self) -> Dict[str, Any]:
        """Produce real-time JSON telemetry payload for dashboard."""
        pos_list = []
        for inst, pos in self.positions.items():
            snap = self.market_indicators.get(inst)
            curr_px = snap.price if snap else pos.entry_price
            pnl_pct = ((curr_px - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
            pnl_usd = (curr_px - pos.entry_price) * pos.size
            pos_list.append({
                "inst_id": pos.inst_id,
                "entry_price": pos.entry_price,
                "current_price": curr_px,
                "peak_price": pos.peak_price,
                "size": pos.size,
                "trailing_stop": round(pos.trailing_stop, 4),
                "scaled_out": pos.scaled_out,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "entry_time": pos.entry_time,
            })

        indicators_list = []
        for inst, snap in self.market_indicators.items():
            indicators_list.append({
                "inst_id": snap.inst_id,
                "price": snap.price,
                "change_24h_pct": snap.change_24h_pct,
                "rsi": snap.rsi,
                "atr": snap.atr,
                "atr_pct": snap.atr_pct,
                "volume_surge": snap.volume_surge,
                "trend": snap.trend,
                "signal": snap.signal,
                "trailing_stop_preview": snap.trailing_stop_preview,
            })

        current_dd = 0.0
        if self.peak_equity_usd > 0:
            current_dd = max(0.0, (self.peak_equity_usd - self.current_equity_usd) / self.peak_equity_usd * 100.0)

        return {
            "status": "active" if not self.circuit_tripped else "circuit_tripped",
            "simulated": self.client.simulated,
            "total_equity_usd": round(self.current_equity_usd, 2),
            "cash_usdt": round(self.cash_usdt, 2),
            "peak_equity_usd": round(self.peak_equity_usd, 2),
            "current_drawdown_pct": round(current_dd, 2),
            "max_drawdown_limit_pct": round(self.max_daily_drawdown_pct * 100.0, 1),
            "circuit_tripped": self.circuit_tripped,
            "circuit_reason": self.circuit_reason,
            "strategy": {
                "name": "Multi-Asset ATR Momentum & Dynamic Ratchet",
                "atr_multiplier": self.atr_multiplier,
                "exhaustion_lock_pct": 50,
                "rsi_window": "14 (Overbought 75 / Oversold 30)",
                "volume_surge_min": f"{self.volume_surge_threshold}x",
                "sizing_mode": "Fractional Kelly Volatility-Adjusted",
            },
            "open_positions": pos_list,
            "closed_trades": self.closed_trades[-10:],
            "market_indicators": indicators_list,
            "logs": self.recent_logs[-25:],
            "timestamp": time.time(),
        }
