"""Unit tests for core/premium_discount.py"""

import pytest
from core.premium_discount import calculate


def test_premium_zone():
    """Price above equilibrium should be in premium zone."""
    result = calculate(swing_high=100, swing_low=80, current_price=95)
    assert result["zone"] == "premium"
    assert result["equilibrium"] == 90.0
    assert result["position_pct"] > 50


def test_discount_zone():
    """Price below equilibrium should be in discount zone."""
    result = calculate(swing_high=100, swing_low=80, current_price=85)
    assert result["zone"] == "discount"
    assert result["position_pct"] < 50


def test_equilibrium_zone():
    """Price exactly at 50% should be equilibrium."""
    result = calculate(swing_high=100, swing_low=80, current_price=90)
    assert result["zone"] == "equilibrium"
    assert result["position_pct"] == 50.0


def test_at_swing_high():
    """Price at swing high = 100% position."""
    result = calculate(swing_high=100, swing_low=80, current_price=100)
    assert result["position_pct"] == 100.0
    assert result["zone"] == "premium"


def test_at_swing_low():
    """Price at swing low = 0% position."""
    result = calculate(swing_high=100, swing_low=80, current_price=80)
    assert result["position_pct"] == 0.0
    assert result["zone"] == "discount"


def test_invalid_swing_levels():
    """High <= low should not crash, returns equilibrium."""
    result = calculate(swing_high=80, swing_low=100, current_price=90)
    assert result["zone"] == "equilibrium"


def test_returns_all_keys():
    result = calculate(100, 80, 90)
    for key in ["swing_high", "swing_low", "equilibrium", "zone", "position_pct"]:
        assert key in result
