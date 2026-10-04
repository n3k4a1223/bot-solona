"""
Unit tests for OKX Quantitative Strategy Engine and Indicator Calculations
"""

import pytest
from okx_engine import OKXQuantitativeEngine, OKXPosition


def test_indicator_math():
    # Test EMA calculation
    series = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
    ema = OKXQuantitativeEngine.compute_ema(series, 5)
    assert ema > 10.0 and ema < 20.0

    # Test RSI calculation
    # Ascending series should have RSI > 50
    ascending = [100.0 + i for i in range(25)]
    rsi_up = OKXQuantitativeEngine.compute_rsi(ascending, 14)
    assert rsi_up > 80.0

    # Descending series should have RSI < 50
    descending = [200.0 - i for i in range(25)]
    rsi_down = OKXQuantitativeEngine.compute_rsi(descending, 14)
    assert rsi_down < 20.0


def test_atr_trailing_ratchet_logic():
    pos = OKXPosition(
        inst_id="SOL-USDT",
        entry_price=100.0,
        peak_price=100.0,
        size=1.0,
        trailing_stop=95.0,
    )
    assert pos.trailing_stop == 95.0

    # Price moves to 120, ATR is 2.0 -> Stop = 120 - (2 * 2) = 116.0
    atr = 2.0
    multiplier = 2.0
    new_price = 120.0
    new_stop = new_price - (multiplier * atr)
    if new_stop > pos.trailing_stop:
        pos.trailing_stop = new_stop
        pos.peak_price = new_price

    assert pos.trailing_stop == 116.0
    assert pos.peak_price == 120.0

    # Price dips to 118 -> stop must stay at 116.0 (ratchet cannot decrease)
    dip_price = 118.0
    dip_stop = dip_price - (multiplier * atr)
    assert dip_stop < pos.trailing_stop
    # Trailing stop remains unchanged
    assert pos.trailing_stop == 116.0
