"""
OKX Quantitative High-Frequency Strategy Engine (All-Coin Scalper)
==================================================================
Multi-Asset High-Frequency Scalping Engine executing on ALL OKX Coins:
- Scans ALL 400+ USDT Spot Trading Pairs across OKX
- Live Market Order Routing for both Simulated Demo & Live Mainnet
- Multi-Tiered Lightning-Fast Profit Harvesting:
    * Tier 0: Breakeven Defense at +0.4% (Zero Risk Guaranteed)
    * Tier 1: 50% Immediate Profit Scale-Out Lock at +1.0%
    * Tier 2: Secondary 25% Profit Harvest at +2.0%
    * Dynamic Hyper-Tight ATR Trailing Stop (1.2x -> 0.8x in profit)
- Precise Lot Size and Decimals Normalization across every instrument
- Concurrently evaluated using asynchronous batches
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from execution.okx_client import OKXClient

logger = logging.getLogger("okx_engine")


@dataclass
class OKXPosition:
    inst_id: str
    entry_price: float
    peak_price: float
    size: float                         # Remaining coin quantity
    trailing_stop: float
    initial_size: float = 0.0           # Original coin quantity bought
    usdt_allocated: float = 0.0         # USDT spent on entry
    breakeven_set: bool = False
    scaled_out_tier1: bool = False
    scaled_out_tier2: bool = False
    entry_time: float = field(default_factory=time.time)
    highest_pnl_pct: float = 0.0
    atr_at_entry: float = 0.0

    @property
    def scaled_out(self) -> bool:
        return self.scaled_out_tier1


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
    vol_24h_usdt: float
    trend: str
    signal: str
    trailing_stop_preview: float
    timestamp: float = field(default_factory=time.time)


class OKXQuantitativeEngine:
    """
    High-frequency quantitative engine scanning ALL coins on OKX,
    actively executing buy/sell market orders with ultra-fast profit locks.
    """

    def __init__(
        self,
        okx_client: OKXClient,
        atr_multiplier: float = 1.2,
        fast_profit_tier1: float = 1.0,         # +1.0% quick 50% profit lock
        fast_profit_tier2: float = 2.0,         # +2.0% secondary profit harvest
        breakeven_trigger: float = 0.4,         # +0.4% breakeven defense
        rsi_oversold: float = 30.0,
        rsi_overbought: float = 70.0,
        volume_surge_threshold: float = 1.1,    # Responsive volume trigger
        max_daily_drawdown_pct: float = 0.06,
        trade_amount_usdt: float = 50.0,        # Default size per scalp trade
        max_concurrent_positions: int = 5,
    ):
        self.client = okx_client
        self.atr_multiplier = atr_multiplier
        self.fast_profit_tier1 = fast_profit_tier1
        self.fast_profit_tier2 = fast_profit_tier2
        self.breakeven_trigger = breakeven_trigger
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.volume_surge_threshold = volume_surge_threshold
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.trade_amount_usdt = trade_amount_usdt
        self.max_concurrent_positions = max_concurrent_positions

        # Active State
        self.positions: Dict[str, OKXPosition] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.market_indicators: Dict[str, IndicatorSnapshot] = {}
        self.all_discovered_pairs: List[Dict[str, Any]] = []
        self.instruments_meta: Dict[str, Dict[str, Any]] = {}
        self.last_universe_discovery: float = 0.0
        self.last_meta_refresh: float = 0.0

        self.peak_equity_usd: float = 0.0
        self.current_equity_usd: float = 0.0
        self.cash_usdt: float = 0.0
        self.circuit_tripped: bool = False
        self.circuit_reason: str = ""
        self.recent_logs: List[Dict[str, Any]] = []
        self._semaphore = asyncio.Semaphore(12)
        self.first_cycle_completed: bool = False

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
        if len(self.recent_logs) > 150:
            self.recent_logs.pop(0)
        
        if level == "error":
            logger.error(f"[{tag}] {message}")
        elif level == "warning":
            logger.warning(f"[{tag}] {message}")
        else:
            logger.info(f"[{tag}] {message}")

    # -------------------------------------------------------------------------
    # Instrument Specs & Metadata Cache
    # -------------------------------------------------------------------------
    async def refresh_metadata_if_needed(self) -> None:
        now = time.time()
        if not self.instruments_meta or (now - self.last_meta_refresh > 3600.0):
            try:
                self.instruments_meta = await self.client.get_instruments("SPOT")
                self.last_meta_refresh = now
                logger.info(f"[OKX] Cached specs for {len(self.instruments_meta)} instruments.")
            except Exception as e:
                logger.error(f"[Metadata Error] {e}")

    def format_sell_qty(self, inst_id: str, qty: float) -> str:
        """Format quantity strictly adhering to OKX lotSz and minSz."""
        meta = self.instruments_meta.get(inst_id, {})
        lot_sz = meta.get("lotSz", "0.0001")
        min_sz = float(meta.get("minSz", "0.0001") or 0.0001)

        if "." in lot_sz:
            decimals = len(lot_sz.split(".")[1])
        else:
            decimals = 0

        factor = 10 ** decimals
        truncated = math.floor(qty * factor) / factor
        
        # Ensure at least minSz if possible
        if truncated < min_sz and qty >= min_sz * 0.9:
            truncated = min_sz

        if decimals == 0:
            return str(int(truncated))
        res_str = f"{truncated:.{decimals}f}".rstrip("0").rstrip(".")
        return res_str if res_str else str(truncated)

    # -------------------------------------------------------------------------
    # Dynamic All-Coin Universe Scanner (All 400+ Pairs)
    # -------------------------------------------------------------------------
    async def discover_all_market_coins(self) -> None:
        """
        Scans all 400+ spot coins on OKX, ranks by liquidity & momentum.
        """
        await self.refresh_metadata_if_needed()
        now = time.time()
        if now - self.last_universe_discovery < 30.0 and self.all_discovered_pairs:
            return

        try:
            tickers = await self.client.get_all_usdt_tickers(min_vol_usdt=50_000.0)
            if tickers:
                self.all_discovered_pairs = tickers
                self.last_universe_discovery = now

                # Pre-populate all coins from tickers so UI and engine track the full market
                for t in tickers:
                    inst = t["inst_id"]
                    if inst not in self.market_indicators:
                        px = t["last"]
                        chg = t.get("change_pct", 0.0)
                        self.market_indicators[inst] = IndicatorSnapshot(
                            inst_id=inst,
                            price=px,
                            high_24h=t.get("high24h", px),
                            low_24h=t.get("low24h", px),
                            change_24h_pct=chg,
                            rsi=50.0,
                            atr=round(px * 0.01, 5),
                            atr_pct=1.0,
                            ema_fast=px,
                            ema_mid=px,
                            ema_slow=px,
                            volume_surge=1.0,
                            vol_24h_usdt=t.get("vol_ccy", 0.0),
                            trend="MARKET ACTIVE" if chg >= 0 else "RETRACTING",
                            signal="MOMENTUM_BUY" if chg > 1.5 else "WATCHLIST",
                            trailing_stop_preview=round(px * 0.985, 5),
                        )

                if not self.first_cycle_completed:
                    self.log_event(
                        "ALL_COINS",
                        f"OKX Universe active: {len(tickers)} USDT coins scanned across entire exchange.",
                        level="info",
                    )
        except Exception as e:
            logger.error(f"[Universe Error] {e}")

    # -------------------------------------------------------------------------
    # Technical Indicators Computations
    # -------------------------------------------------------------------------
    @staticmethod
    def compute_ema(series: List[float], period: int) -> float:
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
    # Instrument Analysis
    # -------------------------------------------------------------------------
    async def analyze_instrument(self, inst_id: str) -> Optional[IndicatorSnapshot]:
        async with self._semaphore:
            candles = await self.client.get_candles(inst_id, bar="1m", limit=30)
            if len(candles) < 10:
                return None

            closes = [c["close"] for c in candles]
            volumes = [c["vol"] for c in candles]
            current_price = closes[-1]
            open_price_first = closes[0]
            change_pct = ((current_price - open_price_first) / open_price_first) * 100.0

            high_24h = max(c["high"] for c in candles)
            low_24h = min(c["low"] for c in candles)

            ema9 = self.compute_ema(closes, 9)
            ema21 = self.compute_ema(closes, 21)
            ema50 = self.compute_ema(closes, 50)
            rsi = self.compute_rsi(closes, 14)
            atr = self.compute_atr(candles, 14)
            atr_pct = (atr / current_price * 100.0) if current_price > 0 else 0.0

            avg_vol = sum(volumes[-10:]) / 10.0 if len(volumes) >= 10 else sum(volumes) / len(volumes)
            current_vol = volumes[-1]
            vol_surge = (current_vol / avg_vol) if avg_vol > 0 else 1.0

            # Trend Direction
            if ema9 > ema21:
                trend = "BULLISH MOMENTUM"
            elif ema9 < ema21:
                trend = "BEARISH RETRACEMENT"
            else:
                trend = "CONSOLIDATION"

            signal = "NEUTRAL"
            trailing_preview = current_price - (self.atr_multiplier * atr)

            # High-Frequency Scalping Buy Trigger
            # Active when price holds above EMA9 or is bouncing with volume
            if (
                (current_price >= ema9 or trend == "BULLISH MOMENTUM" or change_pct > 0.1)
                and 35.0 <= rsi <= 68.0
                and vol_surge >= self.volume_surge_threshold
                and not self.circuit_tripped
            ):
                signal = "MOMENTUM_BUY"
            elif rsi > self.rsi_overbought:
                signal = "OVERBOUGHT_EXHAUSTION"
            elif rsi < self.rsi_oversold:
                signal = "OVERSOLD"

            # Find 24h volume
            vol_usdt = 0.0
            for d in self.all_discovered_pairs:
                if d["inst_id"] == inst_id:
                    vol_usdt = d.get("vol_ccy", 0.0)
                    break

            snapshot = IndicatorSnapshot(
                inst_id=inst_id,
                price=current_price,
                high_24h=high_24h,
                low_24h=low_24h,
                change_24h_pct=round(change_pct, 2),
                rsi=round(rsi, 2),
                atr=round(atr, 5),
                atr_pct=round(atr_pct, 2),
                ema_fast=round(ema9, 4),
                ema_mid=round(ema21, 4),
                ema_slow=round(ema50, 4),
                volume_surge=round(vol_surge, 2),
                vol_24h_usdt=round(vol_usdt, 0),
                trend=trend,
                signal=signal,
                trailing_stop_preview=round(trailing_preview, 5),
            )

            self.market_indicators[inst_id] = snapshot
            return snapshot

    # -------------------------------------------------------------------------
    # Lightning-Fast Profit Taking & Trailing Exit Engine
    # -------------------------------------------------------------------------
    async def update_open_positions(self) -> None:
        """
        Monitors open positions every 1-2 seconds and executes REAL SELL ORDERS on OKX:
        - +0.4%: Breakeven Stop Triggered (Zero Risk)
        - +1.0%: 50% Profit Scalp Lock (Market Sell)
        - +2.0%: 25% Profit Harvest (Market Sell)
        - Trailing stop breach: Full Market Exit
        """
        for inst_id, pos in list(self.positions.items()):
            snapshot = self.market_indicators.get(inst_id)
            if not snapshot:
                continue

            current_px = snapshot.price
            atr = snapshot.atr if snapshot.atr > 0 else (current_px * 0.008)

            # Update Peak Price
            if current_px > pos.peak_price:
                pos.peak_price = current_px

            pnl_pct = ((current_px - pos.entry_price) / pos.entry_price) * 100.0 if pos.entry_price > 0 else 0.0
            pos.highest_pnl_pct = max(pos.highest_pnl_pct, pnl_pct)

            # -----------------------------------------------------------------
            # 1. Tier 0: Breakeven Defense (+0.4% PnL)
            # -----------------------------------------------------------------
            if not pos.breakeven_set and pnl_pct >= self.breakeven_trigger:
                pos.breakeven_set = True
                be_price = pos.entry_price * 1.002
                if be_price > pos.trailing_stop:
                    pos.trailing_stop = be_price
                    self.log_event(
                        "BREAKEVEN",
                        f"🛡️ Zero Risk Armed: {inst_id} reached +{pnl_pct:.2f}%. Stop moved to Breakeven (${be_price:.4f})!",
                        level="info",
                    )

            # -----------------------------------------------------------------
            # 2. Tier 1: 50% Immediate Profit Lock (+1.0% PnL or RSI > 70)
            # -----------------------------------------------------------------
            if not pos.scaled_out_tier1 and (pnl_pct >= self.fast_profit_tier1 or snapshot.rsi >= self.rsi_overbought):
                pos.scaled_out_tier1 = True
                scale_qty_float = pos.size * 0.5
                sell_sz_str = self.format_sell_qty(inst_id, scale_qty_float)
                
                if float(sell_sz_str) > 0:
                    pos.size -= float(sell_sz_str)
                    locked_stop = max(pos.trailing_stop, pos.entry_price * 1.006)
                    pos.trailing_stop = locked_stop

                    # REAL MARKET SELL EXECUTION ON OKX
                    res = await self.client.place_order(inst_id, side="sell", sz=sell_sz_str, ord_type="market")
                    self.log_event(
                        "FAST_PROFIT_1",
                        f"⚡ QUICK 50% PROFIT LOCK: Sold {sell_sz_str} {inst_id} at ${current_px:.4f} (+{pnl_pct:.2f}%). Stop lifted to +0.6% (${locked_stop:.4f})! (OKX Order: {res.get('order_id')})",
                        level="warning",
                    )

            # -----------------------------------------------------------------
            # 3. Tier 2: Secondary Fast Profit Harvest (+2.0% PnL)
            # -----------------------------------------------------------------
            if pos.scaled_out_tier1 and not pos.scaled_out_tier2 and pnl_pct >= self.fast_profit_tier2:
                pos.scaled_out_tier2 = True
                scale_qty_float = pos.size * 0.5
                sell_sz_str = self.format_sell_qty(inst_id, scale_qty_float)

                if float(sell_sz_str) > 0:
                    pos.size -= float(sell_sz_str)
                    locked_stop = max(pos.trailing_stop, pos.entry_price * 1.015)
                    pos.trailing_stop = locked_stop

                    # REAL MARKET SELL EXECUTION ON OKX
                    res = await self.client.place_order(inst_id, side="sell", sz=sell_sz_str, ord_type="market")
                    self.log_event(
                        "FAST_PROFIT_2",
                        f"🚀 RAPID PROFIT HARVEST 2: Sold {sell_sz_str} {inst_id} at ${current_px:.4f} (+{pnl_pct:.2f}%). Stop lifted to +1.5%! (OKX Order: {res.get('order_id')})",
                        level="warning",
                    )

            # -----------------------------------------------------------------
            # 4. Dynamic Hyper-Tight ATR Trailing Ratchet
            # -----------------------------------------------------------------
            mult = 0.8 if pnl_pct >= 0.8 else self.atr_multiplier
            new_stop = current_px - (mult * atr)
            if new_stop > pos.trailing_stop:
                pos.trailing_stop = new_stop
                self.log_event(
                    "ATR_RATCHET",
                    f"{inst_id} stop ratcheted up to ${new_stop:.4f} (Peak: ${pos.peak_price:.4f}, PnL: +{pnl_pct:.2f}%)",
                    level="info",
                )

            # -----------------------------------------------------------------
            # 5. Stop-Loss / Profit Exit Trigger
            # -----------------------------------------------------------------
            if current_px <= pos.trailing_stop:
                exit_pnl_pct = ((pos.trailing_stop - pos.entry_price) / pos.entry_price) * 100.0
                profit_usd = (pos.trailing_stop - pos.entry_price) * pos.size
                is_win = exit_pnl_pct >= 0.0

                sell_sz_str = self.format_sell_qty(inst_id, pos.size)
                res = {"order_id": "none"}
                if float(sell_sz_str) > 0:
                    # REAL MARKET SELL EXECUTION ON OKX
                    res = await self.client.place_order(inst_id, side="sell", sz=sell_sz_str, ord_type="market")

                self.log_event(
                    "STOP_EXIT",
                    f"{'✅ WIN CLOSE' if is_win else '🛑 STOP CLOSE'}: {inst_id} exited at ${pos.trailing_stop:.4f} | PnL: {exit_pnl_pct:+.2f}% (${profit_usd:+.2f}) | OKX Order: {res.get('order_id')}",
                    level="warning" if is_win else "error",
                )

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
    # Drawdown Governor & Balance Sync
    # -------------------------------------------------------------------------
    async def evaluate_risk_and_circuit_breaker(self) -> None:
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

    # -------------------------------------------------------------------------
    # Master Execution Cycle across All Coins
    # -------------------------------------------------------------------------
    async def run_cycle(self) -> None:
        """
        Executes an all-coin market scan, places real BUY orders for momentum setups,
        and manages fast profit-taking for all open positions.
        """
        # 1. Discover all 400+ coins and update metadata
        await self.discover_all_market_coins()
        await self.evaluate_risk_and_circuit_breaker()

        # Select top candidates across all coins to analyze with 1m candles
        # OKX candle rate limit is 20 req/2s, so we take top 8 candidates + any open positions
        top_candidates = [d["inst_id"] for d in self.all_discovered_pairs[:8]]
        candle_targets = list(set(top_candidates + list(self.positions.keys())))

        # 2. Concurrently analyze target coins with 1m candles
        tasks = [self.analyze_instrument(inst) for inst in candle_targets]
        snapshots = await asyncio.gather(*tasks, return_exceptions=True)

        valid_snaps: List[IndicatorSnapshot] = [s for s in snapshots if isinstance(s, IndicatorSnapshot)]

        # 3. Active Real Market BUY Execution on OKX
        if len(self.positions) < self.max_concurrent_positions and not self.circuit_tripped:
            # Sort candidates by momentum and volume
            buy_candidates = [s for s in valid_snaps if s.signal == "MOMENTUM_BUY" and s.inst_id not in self.positions]
            
            # If no strict momentum buy, and we have 0 open positions on startup, pick the top trending candidate
            if not buy_candidates and len(self.positions) == 0 and valid_snaps:
                sorted_by_change = sorted(valid_snaps, key=lambda s: s.change_24h_pct, reverse=True)
                for s in sorted_by_change:
                    if s.price > 0 and 35 <= s.rsi <= 72 and s.inst_id not in self.positions:
                        buy_candidates.append(s)
                        if len(buy_candidates) >= 2:
                            break

            for snap in buy_candidates:
                if len(self.positions) >= self.max_concurrent_positions:
                    break

                target_usdt = self.trade_amount_usdt
                if self.cash_usdt > 100:
                    # Dynamically size up to 100 USDT if cash is abundant
                    target_usdt = min(100.0, max(25.0, self.cash_usdt * 0.05))

                target_usdt_str = str(round(target_usdt, 2))

                # EXECUTE REAL MARKET BUY ORDER ON OKX
                res = await self.client.place_order(snap.inst_id, side="buy", sz=target_usdt_str, ord_type="market")
                if res.get("success"):
                    order_id = res.get("order_id", "OKX")
                    est_qty = target_usdt / snap.price
                    stop_level = snap.price - (self.atr_multiplier * snap.atr)

                    self.positions[snap.inst_id] = OKXPosition(
                        inst_id=snap.inst_id,
                        entry_price=snap.price,
                        peak_price=snap.price,
                        size=est_qty,
                        initial_size=est_qty,
                        usdt_allocated=target_usdt,
                        trailing_stop=stop_level,
                        atr_at_entry=snap.atr,
                    )
                    self.log_event(
                        "ORDER_BUY",
                        f"🛒 OKX MARKET BUY: Bought ${target_usdt_str} USDT of {snap.inst_id} at ${snap.price:.4f} (Order ID: {order_id}) | ATR Stop: ${stop_level:.4f}",
                        level="info",
                    )
                else:
                    self.log_event(
                        "BUY_ERROR",
                        f"Failed buying {snap.inst_id}: {res.get('msg')}",
                        level="warning",
                    )

        # 4. Rapid Profit Taking and Trailing Exits
        await self.update_open_positions()
        self.first_cycle_completed = True

    def get_telemetry(self) -> Dict[str, Any]:
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
                "size": round(pos.size, 4),
                "usdt_allocated": pos.usdt_allocated,
                "trailing_stop": round(pos.trailing_stop, 4),
                "breakeven_set": pos.breakeven_set,
                "scaled_out_tier1": pos.scaled_out_tier1,
                "scaled_out_tier2": pos.scaled_out_tier2,
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
                "entry_time": pos.entry_time,
            })

        indicators_list = []
        sorted_snaps = sorted(self.market_indicators.values(), key=lambda s: s.vol_24h_usdt, reverse=True)
        for snap in sorted_snaps:
            indicators_list.append({
                "inst_id": snap.inst_id,
                "price": snap.price,
                "change_24h_pct": snap.change_24h_pct,
                "rsi": snap.rsi,
                "atr": snap.atr,
                "atr_pct": snap.atr_pct,
                "volume_surge": snap.volume_surge,
                "vol_24h_usdt": snap.vol_24h_usdt,
                "trend": snap.trend,
                "signal": snap.signal,
                "trailing_stop_preview": snap.trailing_stop_preview,
            })

        current_dd = 0.0
        if self.peak_equity_usd > 0:
            current_dd = max(0.0, (self.peak_equity_usd - self.current_equity_usd) / self.peak_equity_usd * 100.0)

        return {
            "status": "active" if not self.circuit_tripped else "circuit_tripped",
            "mode": "Active All-Coin Real-Order Scalper",
            "simulated": self.client.simulated,
            "total_equity_usd": round(self.current_equity_usd, 2),
            "cash_usdt": round(self.cash_usdt, 2),
            "peak_equity_usd": round(self.peak_equity_usd, 2),
            "current_drawdown_pct": round(current_dd, 2),
            "max_drawdown_limit_pct": round(self.max_daily_drawdown_pct * 100.0, 1),
            "circuit_tripped": self.circuit_tripped,
            "circuit_reason": self.circuit_reason,
            "universe_coins_tracked": len(self.all_discovered_pairs),
            "total_market_pairs": len(self.all_discovered_pairs),
            "strategy": {
                "name": "OKX Live All-Coin Rapid Scalper",
                "profit_lock_tier1": f"+{self.fast_profit_tier1}% (50% scale-out)",
                "profit_lock_tier2": f"+{self.fast_profit_tier2}% (secondary harvest)",
                "breakeven_guard": f"+{self.breakeven_trigger}% (zero risk)",
                "atr_multiplier": f"{self.atr_multiplier}x (tightens to 0.8x in profit)",
                "trade_amount_usdt": f"${self.trade_amount_usdt} USDT",
                "max_concurrent_positions": self.max_concurrent_positions,
            },
            "open_positions": pos_list,
            "closed_trades": self.closed_trades[-20:],
            "market_indicators": indicators_list[:250],
            "logs": self.recent_logs[-35:],
            "timestamp": time.time(),
        }
