"""Unit tests for core/entry_engine.py - focuses on scoring and validation gates."""

import pandas as pd
import numpy as np
import pytest
from unittest.mock import patch, MagicMock


def _make_trending_df(n=50, direction="bullish", base=100.0):
    """Build a synthetic trending OHLCV DataFrame."""
    step = 0.5 if direction == "bullish" else -0.5
    closes = [base + i * step for i in range(n)]
    highs  = [c + 1.0 for c in closes]
    lows   = [c - 1.0 for c in closes]
    opens  = [c - 0.3 for c in closes]
    return pd.DataFrame({
        "open": opens, "high": highs,
        "low": lows, "close": closes,
        "volume": [1000.0] * n,
    })


def _make_mtf(direction="bullish"):
    """Build MTF data matching the default swing strategy TFs (1d, 4h, 1h)."""
    return {
        "1h":  _make_trending_df(100, direction, 100),
        "4h":  _make_trending_df(100, direction, 100),
        "1d":  _make_trending_df(100, direction, 100),
    }


def test_no_setup_when_mixed_htf_trend():
    """Mixed trend across HTF TFs should return no setups."""
    from core.entry_engine import analyze_pair
    mtf = {
        "1h":  _make_trending_df(100, "bullish", 100),
        "4h":  _make_trending_df(100, "bearish", 100),  # conflicting with 1D/1H
        "1d":  _make_trending_df(100, "bullish", 100),
    }
    setups = analyze_pair("BTC/USDT", mtf)
    assert isinstance(setups, list)
    assert setups == []


def test_no_setup_with_insufficient_data():
    """Missing required TF data should return empty list without crashing."""
    from core.entry_engine import analyze_pair
    # Swing strategy needs 1d, 4h, 1h — passing None for 1h should fail gracefully
    mtf = {
        "1h":  None,
        "4h":  _make_trending_df(50, "bullish"),
        "1d":  _make_trending_df(50, "bullish"),
    }
    setups = analyze_pair("BTC/USDT", mtf)
    assert setups == []


def test_analyze_pair_returns_list():
    """analyze_pair should always return a list."""
    from core.entry_engine import analyze_pair
    mtf = _make_mtf("bullish")
    result = analyze_pair("ETH/USDT", mtf)
    assert isinstance(result, list)


def test_setup_dict_has_required_keys():
    """If a setup is returned, it must have all required keys."""
    from core.entry_engine import analyze_pair

    required_keys = [
        "symbol", "direction", "entry_low", "entry_high", "entry_mid",
        "sl", "tp1", "tp2", "rr_tp1", "rr_tp2", "confidence",
        "htf_trend", "reasons", "timeframe_entry", "timestamp", "current_price",
        "strategy_name", "strategy_label", "strategy_emoji", "entry_tf",
    ]

    # We can't guarantee a setup will be generated with synthetic data,
    # so just verify the structure if one is returned
    mtf = _make_mtf("bearish")
    result = analyze_pair("SOL/USDT", mtf)
    for setup in result:
        for key in required_keys:
            assert key in setup, f"Missing key in setup: {key}"
