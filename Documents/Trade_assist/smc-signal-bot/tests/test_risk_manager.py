"""Unit tests for core/risk_manager.py"""

import pytest
from core.risk_manager import calculate_rr, calculate_position_size, validate_setup


# ── calculate_rr ──────────────────────────────────────────────────────────────

def test_rr_long_2to1():
    """Long trade: risk 5 points, reward 10 points = 2.0 RR."""
    rr = calculate_rr(entry=100, sl=95, tp=110)
    assert rr == 2.0


def test_rr_short_3to1():
    """Short trade: risk 5 points, reward 15 points = 3.0 RR."""
    rr = calculate_rr(entry=100, sl=105, tp=85)
    assert rr == 3.0


def test_rr_zero_risk():
    """Entry == SL → risk is zero, should return 0."""
    rr = calculate_rr(entry=100, sl=100, tp=110)
    assert rr == 0.0


# ── calculate_position_size ───────────────────────────────────────────────────

def test_position_size_basic():
    """1% risk on 100,000 capital with 2% SL = correct position size."""
    result = calculate_position_size(capital=100_000, risk_pct=1, entry=100, sl=98)
    assert result["risk_amount"] == 1000.0
    assert result["units"] > 0
    # SL distance = 2, so units = 1000/2 = 500
    assert result["units"] == 500.0


def test_position_size_zero_entry():
    """Zero entry price should return zero position."""
    result = calculate_position_size(capital=100_000, risk_pct=1, entry=0, sl=0)
    assert result["units"] == 0


# ── validate_setup ────────────────────────────────────────────────────────────

def test_valid_long_setup():
    """Valid long: sl < entry < tp1 < tp2, rr >= 2."""
    valid, rr1, rr2 = validate_setup(
        entry=100, sl=95, tp1=110, tp2=120,
        direction="bullish", min_rr=2.0
    )
    assert valid is True
    assert rr1 == 2.0    # (110-100)/(100-95)
    assert rr2 == 4.0    # (120-100)/(100-95)


def test_valid_short_setup():
    """Valid short: tp2 < tp1 < entry < sl, rr >= 2."""
    valid, rr1, rr2 = validate_setup(
        entry=100, sl=105, tp1=90, tp2=80,
        direction="bearish", min_rr=2.0
    )
    assert valid is True
    assert rr1 == 2.0
    assert rr2 == 4.0


def test_invalid_rr_below_minimum():
    """Setup where RR < min_rr should be invalid."""
    valid, rr1, rr2 = validate_setup(
        entry=100, sl=95, tp1=103, tp2=105,
        direction="bullish", min_rr=2.0
    )
    assert valid is False
    assert rr2 < 2.0


def test_invalid_long_sl_above_entry():
    """SL above entry for long is invalid."""
    valid, _, _ = validate_setup(
        entry=100, sl=105, tp1=110, tp2=120,
        direction="bullish"
    )
    assert valid is False


def test_invalid_direction():
    """Unknown direction returns invalid."""
    valid, _, _ = validate_setup(
        entry=100, sl=95, tp1=110, tp2=120,
        direction="sideways"
    )
    assert valid is False


def test_boundary_exactly_2rr():
    """Setup with exactly 2.0 RR should be valid."""
    # entry=100, sl=95 → risk=5; tp2 = 100 + 10 = 110
    valid, _, rr2 = validate_setup(
        entry=100, sl=95, tp1=105, tp2=110,
        direction="bullish", min_rr=2.0
    )
    assert valid is True
    assert rr2 == 2.0
