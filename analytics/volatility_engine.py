"""
Real-Time Volatility Engine & ATR Return Analytics
==================================================
Calculates the continuous standard deviation of 1-minute returns, Average
True Range (ATR), and dynamic volatility regime classification to drive
both position sizing and adaptive trailing stop loss bands.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple
import numpy as np

from core.logger import get_logger
from core.types import VolatilityMetrics


@dataclass
class PriceTick:
    """Historical price tick observation."""
    price_sol: float
    timestamp: float


@dataclass
class OHLCBar:
    """Discrete 1-minute OHLC bar."""
    open: float
    high: float
    low: float
    close: float
    timestamp: float


class VolatilityEngine:
    """
    Computes standard deviation of log returns, discrete ATR, and
    regime classification for dynamic risk-adjusted scaling.
    """

    def __init__(
        self,
        lookback_periods: int = 15,  # 15 one-minute intervals
        target_volatility_1m: float = 0.03,  # 3% target 1m return stdev
    ):
        self.lookback_periods = lookback_periods
        self.target_volatility_1m = target_volatility_1m
        self.logger = get_logger()

        # token_mint -> deque of PriceTick
        self._price_ticks: Dict[str, Deque[PriceTick]] = {}
        # token_mint -> deque of OHLCBar
        self._ohlc_bars: Dict[str, Deque[OHLCBar]] = {}

    def record_price(self, token_mint: str, price_sol: float) -> None:
        """Records latest price tick and aggregates into 1-minute OHLC bars."""
        now = time.time()
        if token_mint not in self._price_ticks:
            self._price_ticks[token_mint] = deque(maxlen=600)
            self._ohlc_bars[token_mint] = deque(maxlen=self.lookback_periods * 2)

        self._price_ticks[token_mint].append(PriceTick(price_sol=price_sol, timestamp=now))
        self._update_ohlc_bars(token_mint, price_sol, now)

    def _update_ohlc_bars(self, token_mint: str, price_sol: float, now: float) -> None:
        """Constructs discrete 60-second rolling bars."""
        bars = self._ohlc_bars[token_mint]
        current_minute_bucket = int(now // 60) * 60

        if not bars or bars[-1].timestamp < current_minute_bucket:
            # Create new bar
            new_bar = OHLCBar(
                open=price_sol,
                high=price_sol,
                low=price_sol,
                close=price_sol,
                timestamp=current_minute_bucket,
            )
            bars.append(new_bar)
        else:
            # Update existing bar
            active_bar = bars[-1]
            active_bar.high = max(active_bar.high, price_sol)
            active_bar.low = min(active_bar.low, price_sol)
            active_bar.close = price_sol

    def calculate_volatility(self, token_mint: str) -> VolatilityMetrics:
        """
        Computes 1-minute return standard deviation and ATR.
        """
        bars = self._ohlc_bars.get(token_mint)
        ticks = self._price_ticks.get(token_mint)

        current_price = ticks[-1].price_sol if ticks else 0.0

        if not bars or len(bars) < 3:
            # Not enough bar history; use micro-tick standard deviation fallback
            return self._calculate_tick_fallback(token_mint, current_price)

        # ---------------------------------------------------------------------
        # 1. Discrete 1-Minute Log Returns: r_t = ln(Close_t / Close_{t-1})
        # ---------------------------------------------------------------------
        closes = [b.close for b in bars if b.close > 0]
        if len(closes) < 3:
            return self._calculate_tick_fallback(token_mint, current_price)

        log_returns = np.diff(np.log(closes))
        std_1m = float(np.std(log_returns, ddof=1)) if len(log_returns) > 1 else 0.03

        # ---------------------------------------------------------------------
        # 2. Average True Range (ATR)
        # TR = max(H - L, |H - C_prev|, |L - C_prev|)
        # ---------------------------------------------------------------------
        true_ranges = []
        for i in range(1, len(bars)):
            high = bars[i].high
            low = bars[i].low
            prev_close = bars[i - 1].close
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        atr_sol = float(np.mean(true_ranges)) if true_ranges else (current_price * 0.04)

        # ---------------------------------------------------------------------
        # 3. Volatility Regime Classification
        # ---------------------------------------------------------------------
        if std_1m < 0.015:
            regime = "LOW"
        elif std_1m < 0.045:
            regime = "NORMAL"
        elif std_1m < 0.090:
            regime = "HIGH"
        else:
            regime = "EXTREME"

        # ---------------------------------------------------------------------
        # 4. Volatility Scaling Factor (Target Vol / Realized Vol)
        # Normalizes risk exposure: scales down in wild swings, up in stable trends
        # ---------------------------------------------------------------------
        floor_vol = 0.01  # Prevent divide-by-zero
        scale_factor = self.target_volatility_1m / max(std_1m, floor_vol)
        # Clamp scale factor between 0.4x and 1.6x
        scale_factor = float(np.clip(scale_factor, 0.4, 1.6))

        return VolatilityMetrics(
            token_mint=token_mint,
            std_1m_returns=std_1m,
            atr_sol=atr_sol,
            current_price_sol=current_price,
            regime=regime,
            volatility_scale_factor=scale_factor,
        )

    def _calculate_tick_fallback(self, token_mint: str, current_price: float) -> VolatilityMetrics:
        """Fallback when full 1-minute bars are still accumulating."""
        ticks = self._price_ticks.get(token_mint, deque())
        if len(ticks) >= 4:
            prices = [t.price_sol for t in ticks if t.price_sol > 0]
            log_diffs = np.diff(np.log(prices))
            std_ticks = float(np.std(log_diffs, ddof=1)) if len(log_diffs) > 1 else 0.03
        else:
            std_ticks = 0.035

        atr_sol = current_price * max(std_ticks, 0.02)
        scale_factor = float(np.clip(self.target_volatility_1m / max(std_ticks, 0.01), 0.5, 1.5))

        return VolatilityMetrics(
            token_mint=token_mint,
            std_1m_returns=std_ticks,
            atr_sol=atr_sol,
            current_price_sol=current_price,
            regime="NORMAL",
            volatility_scale_factor=scale_factor,
        )
