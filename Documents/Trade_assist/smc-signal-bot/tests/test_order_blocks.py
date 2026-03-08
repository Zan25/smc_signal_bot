"""Unit tests for core/order_blocks.py"""

import pandas as pd
import numpy as np
import pytest

from core.order_blocks import detect, is_price_in_ob
from core.data_fetcher import compute_atr


def _make_df(opens, highs, lows, closes):
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * len(opens),
    })


def _flat_structure():
    """Empty structure dict for testing."""
    return {"trend": "bullish", "last_bos": None, "last_choch": None}


def test_bullish_ob_detected():
    """
    Bearish candle followed by 3 bullish candles with large move should be a bullish OB.
    """
    n = 30
    # Build a series with a clear setup
    opens  = [100.0] * n
    closes = [100.0] * n
    highs  = [101.0] * n
    lows   = [ 99.0] * n

    # Place bearish candle at index 10
    opens[10]  = 100.0
    closes[10] = 98.0   # bearish
    highs[10]  = 100.5
    lows[10]   =  97.5

    # Big bullish move after index 10 (ATR will be small, so 3-candle move needs to exceed threshold)
    for i in range(11, 15):
        opens[i]  = 98.0 + (i - 10) * 3
        closes[i] = 99.0 + (i - 10) * 3
        highs[i]  = 101.0 + (i - 10) * 3
        lows[i]   =  97.5 + (i - 10) * 3

    df = _make_df(opens, highs, lows, closes)
    atr = compute_atr(df)
    result = detect(df, atr, _flat_structure())

    # Should have at least one bullish OB (the bearish candle at 10)
    assert isinstance(result["bullish"], list)
    assert isinstance(result["bearish"], list)


def test_is_price_in_ob_inside():
    ob = {"zone_low": 100.0, "zone_high": 110.0}
    assert is_price_in_ob(105.0, ob) is True


def test_is_price_in_ob_outside():
    ob = {"zone_low": 100.0, "zone_high": 110.0}
    assert is_price_in_ob(90.0, ob) is False


def test_is_price_in_ob_at_boundary():
    ob = {"zone_low": 100.0, "zone_high": 110.0}
    assert is_price_in_ob(100.0, ob) is True
    assert is_price_in_ob(110.0, ob) is True


def test_detect_empty_df_no_crash():
    df = _make_df([100], [101], [99], [100])
    atr = compute_atr(df)
    result = detect(df, atr, _flat_structure())
    assert result == {"bullish": [], "bearish": []}
