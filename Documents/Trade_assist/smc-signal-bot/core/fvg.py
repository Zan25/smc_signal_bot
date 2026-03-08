"""
Fair Value Gap (FVG) detection module.
Detects price gaps between 3 consecutive candles and tracks fill status.

FVG = imbalance area where candle 1 and candle 3 do not overlap.
"""

import pandas as pd
from utils.logger import get_logger

logger = get_logger(__name__)

_MAX_FVG_TRACKED = 10


def detect(df: pd.DataFrame) -> dict:
    """
    Detect all active (unfilled or <50% filled) Fair Value Gaps.

    Bullish FVG: high[i-2] < low[i]  → zone = (high[i-2], low[i])
    Bearish FVG: low[i-2] > high[i]  → zone = (high[i], low[i-2])

    An FVG is removed when price has filled > 50% of its range.

    Args:
        df: OHLCV DataFrame (minimum 3 candles)

    Returns:
        Dict with:
            bullish (list): active bullish FVGs
            bearish (list): active bearish FVGs
        Each FVG entry: {zone_low, zone_high, midpoint, index, filled_pct}
    """
    bullish_fvgs = []
    bearish_fvgs = []
    current_price = float(df["close"].iloc[-1])

    if len(df) < 3:
        return {"bullish": [], "bearish": []}

    # Scan for FVGs (skip last 1 candle since we check i as the rightmost of 3)
    for i in range(2, len(df)):
        h_left = float(df["high"].iloc[i - 2])
        l_left = float(df["low"].iloc[i - 2])
        h_right = float(df["high"].iloc[i])
        l_right = float(df["low"].iloc[i])

        # Bullish FVG
        if h_left < l_right:
            zone_low = h_left
            zone_high = l_right
            gap_size = zone_high - zone_low
            if gap_size > 0:
                # Bullish FVG fills when price comes back DOWN into the zone.
                # price above zone_high → not filled (0%)
                # price inside zone   → partially filled from top
                # price below zone_low → fully filled (100%)
                if current_price >= zone_high:
                    filled_pct = 0.0
                elif current_price <= zone_low:
                    filled_pct = 1.0
                else:
                    filled_pct = (zone_high - current_price) / gap_size
                if filled_pct < 0.5 and len(bullish_fvgs) < _MAX_FVG_TRACKED:
                    bullish_fvgs.append({
                        "zone_low": zone_low,
                        "zone_high": zone_high,
                        "midpoint": (zone_low + zone_high) / 2,
                        "index": int(i - 1),  # middle candle index
                        "filled_pct": round(filled_pct, 3),
                    })

        # Bearish FVG
        if l_left > h_right:
            zone_high = l_left
            zone_low = h_right
            gap_size = zone_high - zone_low
            if gap_size > 0:
                # Calculate how much price has filled the gap (from below)
                fill_from_bottom = max(0.0, current_price - zone_low)
                filled_pct = min(1.0, fill_from_bottom / gap_size) if current_price < zone_high else 1.0
                if filled_pct < 0.5 and len(bearish_fvgs) < _MAX_FVG_TRACKED:
                    bearish_fvgs.append({
                        "zone_low": zone_low,
                        "zone_high": zone_high,
                        "midpoint": (zone_low + zone_high) / 2,
                        "index": int(i - 1),
                        "filled_pct": round(filled_pct, 3),
                    })

    # Sort by recency (most recent first)
    bullish_fvgs.sort(key=lambda x: x["index"], reverse=True)
    bearish_fvgs.sort(key=lambda x: x["index"], reverse=True)

    logger.debug(f"Detected {len(bullish_fvgs)} bullish FVGs, {len(bearish_fvgs)} bearish FVGs")
    return {"bullish": bullish_fvgs, "bearish": bearish_fvgs}


def zones_overlap(
    zone_a: tuple[float, float],
    zone_b: tuple[float, float],
    tolerance: float = 0.001,
) -> bool:
    """
    Check whether two price zones overlap (with optional tolerance).

    Args:
        zone_a: (low, high) tuple
        zone_b: (low, high) tuple
        tolerance: Fraction of zone_a width to extend overlap check

    Returns:
        True if the zones overlap
    """
    a_low, a_high = zone_a
    b_low, b_high = zone_b
    buf = (a_high - a_low) * tolerance
    return not (a_high + buf < b_low or b_high < a_low - buf)
