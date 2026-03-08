"""
Liquidity detection module.
Detects Equal Highs (EQH), Equal Lows (EQL), and Weak/Strong Highs/Lows.

Liquidity pools sit above EQH and below EQL (clusters of stop-loss orders).
Market tends to sweep these levels before reversing.
"""

import pandas as pd
import numpy as np
from utils.logger import get_logger
from config.settings import EQH_EQL_TOLERANCE, LIQUIDITY_PROXIMITY_PCT

logger = get_logger(__name__)


def _group_equal_levels(
    levels: list[float],
    tolerance: float = EQH_EQL_TOLERANCE,
) -> list[dict]:
    """
    Group price levels that are within tolerance of each other.

    Args:
        levels: List of price values to group
        tolerance: Maximum % difference to be considered 'equal'

    Returns:
        List of grouped level dicts with representative level and touch count
    """
    if not levels:
        return []

    sorted_levels = sorted(levels)
    groups = []
    current_group = [sorted_levels[0]]

    for level in sorted_levels[1:]:
        ref = current_group[0]
        if abs(level - ref) / ref <= tolerance:
            current_group.append(level)
        else:
            if len(current_group) >= 2:
                groups.append({
                    "level": sum(current_group) / len(current_group),  # average
                    "touch_count": len(current_group),
                })
            current_group = [level]

    if len(current_group) >= 2:
        groups.append({
            "level": sum(current_group) / len(current_group),
            "touch_count": len(current_group),
        })

    return groups


def detect_equal_levels(
    swing_highs: list[float],
    swing_lows: list[float],
    tolerance: float = EQH_EQL_TOLERANCE,
) -> tuple[list[dict], list[dict]]:
    """
    Detect Equal Highs and Equal Lows from swing level lists.

    Args:
        swing_highs: List of confirmed swing high prices
        swing_lows: List of confirmed swing low prices
        tolerance: EQH/EQL grouping tolerance (default 0.2%)

    Returns:
        (eq_highs, eq_lows) - lists of grouped equal level dicts
    """
    eq_highs = _group_equal_levels(swing_highs, tolerance)
    eq_lows = _group_equal_levels(swing_lows, tolerance)
    return eq_highs, eq_lows


def detect_weak_strong(
    df: pd.DataFrame,
    swing_highs_series: pd.Series,
    swing_lows_series: pd.Series,
) -> dict:
    """
    Detect weak and strong highs/lows based on liquidity sweep behavior.

    Weak High: formed without sweeping liquidity above → unprotected, likely to be targeted
    Strong High: formed after sweeping liquidity above → protected
    (Inverted logic for lows)

    Args:
        df: OHLCV DataFrame
        swing_highs_series: pd.Series of confirmed swing highs (NaN where no swing)
        swing_lows_series: pd.Series of confirmed swing lows (NaN where no swing)

    Returns:
        Dict with weak_highs, strong_highs, weak_lows, strong_lows lists
    """
    highs_idx = swing_highs_series.dropna().index.tolist()
    lows_idx = swing_lows_series.dropna().index.tolist()

    weak_highs = []
    strong_highs = []
    weak_lows = []
    strong_lows = []

    # For each swing high, check if price swept above the prior swing high before forming it
    for pos, idx in enumerate(highs_idx):
        sh_price = float(swing_highs_series.iloc[idx])
        # Look back for the previous swing high
        if pos == 0:
            weak_highs.append(sh_price)
            continue
        prev_sh = float(swing_highs_series.iloc[highs_idx[pos - 1]])
        # Check if any candle between the two swing highs exceeded the prior swing high
        prev_idx = highs_idx[pos - 1]
        candles_between = df["high"].iloc[prev_idx:idx]
        swept = any(float(h) > prev_sh for h in candles_between)
        if swept:
            strong_highs.append(sh_price)
        else:
            weak_highs.append(sh_price)

    # For each swing low, check if price swept below the prior swing low before forming it
    for pos, idx in enumerate(lows_idx):
        sl_price = float(swing_lows_series.iloc[idx])
        if pos == 0:
            weak_lows.append(sl_price)
            continue
        prev_sl = float(swing_lows_series.iloc[lows_idx[pos - 1]])
        prev_idx = lows_idx[pos - 1]
        candles_between = df["low"].iloc[prev_idx:idx]
        swept = any(float(l) < prev_sl for l in candles_between)
        if swept:
            strong_lows.append(sl_price)
        else:
            weak_lows.append(sl_price)

    return {
        "weak_highs": weak_highs,
        "strong_highs": strong_highs,
        "weak_lows": weak_lows,
        "strong_lows": strong_lows,
    }


def analyze(
    df: pd.DataFrame,
    swing_highs_series: pd.Series,
    swing_lows_series: pd.Series,
) -> dict:
    """
    Full liquidity analysis: EQH, EQL, and weak/strong highs and lows.

    Args:
        df: OHLCV DataFrame
        swing_highs_series: Confirmed swing highs from market_structure
        swing_lows_series: Confirmed swing lows from market_structure

    Returns:
        Dict with:
            eq_highs (list[dict]): equal high clusters
            eq_lows (list[dict]): equal low clusters
            weak_highs (list[float])
            strong_highs (list[float])
            weak_lows (list[float])
            strong_lows (list[float])
    """
    sh_vals = swing_highs_series.dropna().tolist()
    sl_vals = swing_lows_series.dropna().tolist()

    eq_highs, eq_lows = detect_equal_levels(sh_vals, sl_vals)
    weak_strong = detect_weak_strong(df, swing_highs_series, swing_lows_series)

    logger.debug(
        f"Liquidity: {len(eq_highs)} EQH, {len(eq_lows)} EQL, "
        f"{len(weak_strong['weak_highs'])} weak highs, {len(weak_strong['weak_lows'])} weak lows"
    )

    return {
        "eq_highs": eq_highs,
        "eq_lows": eq_lows,
        **weak_strong,
    }


def is_near_liquidity(
    price: float,
    liquidity_data: dict,
    direction: str,
    proximity_pct: float = LIQUIDITY_PROXIMITY_PCT,
) -> tuple[bool, dict | None]:
    """
    Check if a price is near a liquidity target relevant to the trade direction.

    For SHORT: look for EQH or weak highs above (liquidity to sweep)
    For LONG: look for EQL or weak lows below (liquidity to sweep)

    Args:
        price: Current price
        liquidity_data: Output from analyze()
        direction: 'bullish' (long) or 'bearish' (short)
        proximity_pct: Maximum % distance to be considered 'near'

    Returns:
        (is_near, target_dict) where target_dict has 'level' and 'type'
    """
    targets = []

    if direction == "bearish":
        # Short setup: liquidity above price (EQH, weak highs)
        for eqh in liquidity_data.get("eq_highs", []):
            if eqh["level"] > price:
                dist = (eqh["level"] - price) / price
                if dist <= proximity_pct * 10:  # within 10x proximity
                    targets.append({"level": eqh["level"], "type": "EQH", "dist": dist})
        for wh in liquidity_data.get("weak_highs", []):
            if wh > price:
                dist = (wh - price) / price
                if dist <= proximity_pct * 10:
                    targets.append({"level": wh, "type": "Weak High", "dist": dist})

    elif direction == "bullish":
        # Long setup: liquidity below price (EQL, weak lows)
        for eql in liquidity_data.get("eq_lows", []):
            if eql["level"] < price:
                dist = (price - eql["level"]) / price
                if dist <= proximity_pct * 10:
                    targets.append({"level": eql["level"], "type": "EQL", "dist": dist})
        for wl in liquidity_data.get("weak_lows", []):
            if wl < price:
                dist = (price - wl) / price
                if dist <= proximity_pct * 10:
                    targets.append({"level": wl, "type": "Weak Low", "dist": dist})

    if targets:
        # Return closest target
        closest = min(targets, key=lambda x: x["dist"])
        return True, closest

    return False, None
