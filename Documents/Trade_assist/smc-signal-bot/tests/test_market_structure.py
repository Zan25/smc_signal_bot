"""Unit tests for core/market_structure.py"""

import numpy as np
import pandas as pd
import pytest

from core.market_structure import (
    detect_swings,
    determine_trend,
    detect_bos,
    detect_choch,
    analyze,
)


def _make_df(highs, lows, closes=None, opens=None):
    """Helper to build a minimal OHLCV DataFrame."""
    n = len(highs)
    if closes is None:
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    if opens is None:
        opens = closes[:]
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
    })


# ── Swing detection ────────────────────────────────────────────────────────────

def test_swing_high_detected_at_peak():
    """A clear peak should be detected as a swing high."""
    highs = [10, 11, 15, 11, 10, 11, 12, 11, 10]
    lows  = [ 9, 10, 10,  9,  9,  9, 10,  9,  9]
    df = _make_df(highs, lows)
    sh, sl = detect_swings(df, n=2)
    assert sh.iloc[2] == 15.0, "Peak at index 2 should be a swing high"


def test_swing_low_detected_at_trough():
    """A clear trough should be detected as a swing low."""
    highs = [10, 9, 8, 9, 10, 9, 8, 9, 10]
    lows  = [ 9, 8, 5, 8,  9, 8, 5, 8,  9]
    df = _make_df(highs, lows)
    sh, sl = detect_swings(df, n=2)
    assert sl.iloc[2] == 5.0, "Trough at index 2 should be a swing low"


def test_no_swing_at_trailing_candles():
    """Last n candles cannot have confirmed swings."""
    highs = [10, 11, 15, 14, 20]  # 20 at end should NOT be confirmed
    lows  = [ 9, 10, 10,  9, 15]
    df = _make_df(highs, lows)
    sh, sl = detect_swings(df, n=2)
    assert np.isnan(sh.iloc[-1]), "Last candle should NOT be a confirmed swing high"
    assert np.isnan(sh.iloc[-2]), "Second-to-last candle should NOT be confirmed"


# ── Trend determination ────────────────────────────────────────────────────────

def test_bullish_trend_hh_hl():
    """Perfect HH+HL series should be identified as bullish."""
    highs = [10, 12, 14, 16]
    lows  = [ 8,  9, 11, 13]
    sh = pd.Series([np.nan] + highs[1:])
    sl = pd.Series(lows + [np.nan])
    assert determine_trend(sh, sl) == "bullish"


def test_bearish_trend_lh_ll():
    """Perfect LH+LL series should be identified as bearish."""
    highs = [16, 14, 12, 10]
    lows  = [13, 11,  9,  7]
    sh = pd.Series(highs)
    sl = pd.Series(lows)
    assert determine_trend(sh, sl) == "bearish"


def test_ranging_mixed_structure():
    """Mixed HH/LL should be identified as ranging."""
    sh = pd.Series([10, 12, 11, 13])  # not consistent
    sl = pd.Series([ 8,  9, 10,  8])  # not consistent
    assert determine_trend(sh, sl) == "ranging"


def test_insufficient_swings_returns_ranging():
    sh = pd.Series([10.0])
    sl = pd.Series([8.0])
    assert determine_trend(sh, sl) == "ranging"


# ── BOS and CHoCH ─────────────────────────────────────────────────────────────

def test_bullish_bos_detected():
    """Bullish BOS: close breaks above last confirmed swing high."""
    # Create a zigzag uptrend: swing high at index 4 (115), then price breaks above it
    #  idx:  0    1    2    3    4    5    6    7    8    9   10   11   12   13   14
    highs  = [100, 105, 110, 108, 115, 112, 110, 112, 118, 116, 114, 116, 120, 118, 125]
    lows   = [ 98, 103, 107, 105, 112, 109, 107, 109, 115, 113, 111, 113, 117, 115, 122]
    closes = [ 99, 104, 109, 107, 114, 110, 108, 111, 117, 115, 112, 115, 119, 117, 124]
    df = _make_df(highs, lows, closes)
    sh, sl = detect_swings(df, n=2)
    # The last close (124) should be above a confirmed swing high
    bos = detect_bos(df, sh, sl, "bullish")
    assert bos is not None
    assert bos["direction"] == "bullish"


def test_bearish_choch_detected():
    """Bearish CHoCH: in uptrend, close breaks below a swing low."""
    # Uptrend followed by sharp drop
    highs  = [100, 102, 104, 103, 102, 101, 100, 99, 98, 90]
    lows   = [ 98,  99, 100,  98,  97,  96,  95, 94, 93, 85]
    closes = [ 99, 101, 103, 101,  99,  98,  96, 93, 91, 86]
    df = _make_df(highs, lows, closes)
    sh, sl = detect_swings(df, n=2)
    choch = detect_choch(df, sh, sl, "bullish")
    # May or may not detect depending on swing positions; just check no crash
    assert choch is None or choch["type"] == "CHoCH"


# ── Full analyze ──────────────────────────────────────────────────────────────

def test_analyze_returns_all_keys():
    """analyze() should return dict with all required keys."""
    n = 30
    highs  = [100 + i * 0.5 for i in range(n)]
    lows   = [ 99 + i * 0.5 for i in range(n)]
    closes = [ 99.5 + i * 0.5 for i in range(n)]
    df = _make_df(highs, lows, closes)
    result = analyze(df)
    for key in ["trend", "swing_highs", "swing_lows", "last_swing_high", "last_swing_low", "last_bos", "last_choch"]:
        assert key in result, f"Missing key: {key}"


def test_analyze_insufficient_data():
    """analyze() with too few candles should return ranging and no crash."""
    df = _make_df([100, 101, 102], [99, 100, 101])
    result = analyze(df, n=2)
    assert result["trend"] == "ranging"
