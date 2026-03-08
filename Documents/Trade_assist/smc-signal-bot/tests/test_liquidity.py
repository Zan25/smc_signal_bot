"""Unit tests for core/liquidity.py"""

import pandas as pd
import numpy as np
import pytest

from core.liquidity import detect_equal_levels, is_near_liquidity


def test_eqh_detected():
    """Three swing highs within 0.2% should form an EQH cluster."""
    highs = [100.0, 100.1, 100.15]  # all within 0.2% of each other
    lows  = [80.0]
    eq_highs, eq_lows = detect_equal_levels(highs, lows)
    assert len(eq_highs) == 1
    assert eq_highs[0]["touch_count"] == 3
    assert 99.9 < eq_highs[0]["level"] < 100.2


def test_eql_detected():
    """Three swing lows within 0.2% should form an EQL cluster."""
    highs = [120.0]
    lows  = [80.0, 80.1, 80.05]
    eq_highs, eq_lows = detect_equal_levels(highs, lows)
    assert len(eq_lows) == 1
    assert eq_lows[0]["touch_count"] == 3


def test_no_eqh_if_spread_too_large():
    """Highs more than 0.2% apart should NOT form EQH."""
    highs = [100.0, 101.0, 102.0]  # 1% apart
    lows  = []
    eq_highs, _ = detect_equal_levels(highs, lows)
    assert len(eq_highs) == 0


def test_single_level_no_group():
    """A single level cannot form a group (needs at least 2)."""
    eq_highs, _ = detect_equal_levels([100.0], [])
    assert len(eq_highs) == 0


def test_is_near_liquidity_bearish():
    """EQH above price should be flagged as near liquidity for bearish setup."""
    liq_data = {
        "eq_highs": [{"level": 105.0, "touch_count": 2}],
        "eq_lows": [],
        "weak_highs": [],
        "weak_lows": [],
    }
    near, target = is_near_liquidity(100.0, liq_data, direction="bearish")
    assert near is True
    assert target["type"] == "EQH"


def test_is_near_liquidity_bullish():
    """EQL below price should be flagged as near liquidity for bullish setup."""
    liq_data = {
        "eq_highs": [],
        "eq_lows": [{"level": 95.0, "touch_count": 2}],
        "weak_highs": [],
        "weak_lows": [],
    }
    near, target = is_near_liquidity(100.0, liq_data, direction="bullish")
    assert near is True
    assert target["type"] == "EQL"


def test_is_not_near_when_too_far():
    """Liquidity level that is very far away should not be flagged."""
    liq_data = {
        "eq_highs": [{"level": 200.0, "touch_count": 2}],
        "eq_lows": [],
        "weak_highs": [],
        "weak_lows": [],
    }
    near, _ = is_near_liquidity(100.0, liq_data, direction="bearish")
    # 200 is 100% away from 100 — well outside proximity range
    assert near is False
