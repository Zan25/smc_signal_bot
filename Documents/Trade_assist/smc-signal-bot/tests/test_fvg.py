"""Unit tests for core/fvg.py"""

import pandas as pd
import pytest

from core.fvg import detect, zones_overlap


def _make_df(highs, lows, closes=None):
    n = len(highs)
    if closes is None:
        closes = [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({
        "open": closes,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": [1000.0] * n,
    })


# ── Bullish FVG ────────────────────────────────────────────────────────────────

def test_bullish_fvg_detected():
    """Three candles where high[0] < low[2] creates a bullish FVG."""
    # Candle 0: high=100, Candle 1: big body, Candle 2: low=105 → gap 100-105
    highs  = [100, 103, 110]
    lows   = [ 98, 101, 105]
    closes = [ 99, 102, 108]
    df = _make_df(highs, lows, closes)
    result = detect(df)
    assert len(result["bullish"]) == 1
    fvg = result["bullish"][0]
    assert fvg["zone_low"] == 100.0   # high[0]
    assert fvg["zone_high"] == 105.0  # low[2]


def test_bullish_fvg_not_detected_when_overlap():
    """No bullish FVG when candles overlap."""
    highs  = [100, 103, 102]  # high[0]=100, low[2]=99 → overlap
    lows   = [ 98, 101,  99]
    closes = [ 99, 102, 100]
    df = _make_df(highs, lows, closes)
    result = detect(df)
    assert len(result["bullish"]) == 0


# ── Bearish FVG ────────────────────────────────────────────────────────────────

def test_bearish_fvg_detected():
    """Three candles where low[0] > high[2] creates a bearish FVG."""
    # Candle 0: low=105, Candle 2: high=100 → gap 100-105
    highs  = [110, 107, 100]
    lows   = [105, 102,  98]
    closes = [108, 104,  99]
    df = _make_df(highs, lows, closes)
    result = detect(df)
    assert len(result["bearish"]) == 1
    fvg = result["bearish"][0]
    assert fvg["zone_low"] == 100.0   # high[2]
    assert fvg["zone_high"] == 105.0  # low[0]


def test_no_fvg_with_single_candle():
    """Fewer than 3 candles should return empty lists."""
    df = _make_df([100], [99])
    result = detect(df)
    assert result["bullish"] == []
    assert result["bearish"] == []


# ── zones_overlap ─────────────────────────────────────────────────────────────

def test_zones_overlap_true():
    assert zones_overlap((100, 110), (105, 115)) is True


def test_zones_overlap_false():
    assert zones_overlap((100, 110), (115, 125)) is False


def test_zones_overlap_touching():
    """Zones that just touch should overlap (within tolerance)."""
    assert zones_overlap((100, 110), (110, 120)) is True


def test_zones_no_overlap_with_gap():
    assert zones_overlap((100, 105), (110, 120), tolerance=0) is False
