"""
Order block detection module.
Detects bullish and bearish order blocks and tracks their mitigation status.

Order Block = the last candle in the opposite direction before an impulsive move.
"""

import pandas as pd
import numpy as np
from utils.logger import get_logger
from config.settings import (
    OB_MAX_TRACKED,
    OB_MOVE_MULTIPLIER,
    OB_PRICE_FLOOR_PCT,
)

logger = get_logger(__name__)


def _is_impulsive_move(
    df: pd.DataFrame,
    start_idx: int,
    direction: str,
    atr: pd.Series,
) -> bool:
    """
    Check if there is an impulsive move starting from start_idx.

    An impulsive move is defined as:
    - Move distance > OB_MOVE_MULTIPLIER * ATR(14)
    - OR the move causes a Break of Structure (high/low breaks prior swing)

    Args:
        df: OHLCV DataFrame
        start_idx: Index where the move begins
        direction: 'bullish' or 'bearish'
        atr: ATR series

    Returns:
        True if impulsive move detected
    """
    if start_idx + 3 >= len(df):
        return False

    atr_val = float(atr.iloc[start_idx]) if not pd.isna(atr.iloc[start_idx]) else 0
    price = float(df["close"].iloc[start_idx])
    # Floor ATR at minimum % of price
    atr_val = max(atr_val, price * OB_PRICE_FLOOR_PCT)
    threshold = OB_MOVE_MULTIPLIER * atr_val

    # Check 3-candle move after start
    end_idx = min(start_idx + 3, len(df) - 1)

    if direction == "bullish":
        move = float(df["high"].iloc[end_idx]) - float(df["low"].iloc[start_idx])
    else:
        move = float(df["high"].iloc[start_idx]) - float(df["low"].iloc[end_idx])

    return move >= threshold


def _check_mitigation(
    current_price: float,
    zone_low: float,
    zone_high: float,
) -> bool:
    """Return True if current price closes inside the OB zone (mitigated)."""
    return zone_low <= current_price <= zone_high


def detect(
    df: pd.DataFrame,
    atr: pd.Series,
    structure: dict,
) -> dict:
    """
    Detect active (unmitigated) order blocks in a DataFrame.

    Args:
        df: OHLCV DataFrame
        atr: ATR series computed from the same DataFrame
        structure: Output from market_structure.analyze()

    Returns:
        Dict with:
            bullish (list): active bullish OBs sorted by recency
            bearish (list): active bearish OBs sorted by recency
        Each OB entry: {zone_low, zone_high, index, midpoint, mitigated}
    """
    bullish_obs = []
    bearish_obs = []
    current_price = float(df["close"].iloc[-1])

    # Scan backwards, skip last 3 candles (need lookahead to confirm move)
    scan_end = len(df) - 3
    if scan_end < 10:
        return {"bullish": [], "bearish": []}

    for i in range(scan_end - 1, max(scan_end - 100, 0), -1):
        candle_open = float(df["open"].iloc[i])
        candle_close = float(df["close"].iloc[i])
        is_bullish_candle = candle_close > candle_open
        is_bearish_candle = candle_close < candle_open

        # Bullish OB: bearish candle before impulsive bullish move
        if (
            is_bearish_candle
            and len(bullish_obs) < OB_MAX_TRACKED
            and _is_impulsive_move(df, i + 1, "bullish", atr)
        ):
            zone_low = min(candle_open, candle_close)
            zone_high = max(candle_open, candle_close)
            mitigated = _check_mitigation(current_price, zone_low, zone_high)
            if not mitigated:
                bullish_obs.append({
                    "zone_low": zone_low,
                    "zone_high": zone_high,
                    "midpoint": (zone_low + zone_high) / 2,
                    "index": int(i),
                    "mitigated": False,
                })

        # Bearish OB: bullish candle before impulsive bearish move
        if (
            is_bullish_candle
            and len(bearish_obs) < OB_MAX_TRACKED
            and _is_impulsive_move(df, i + 1, "bearish", atr)
        ):
            zone_low = min(candle_open, candle_close)
            zone_high = max(candle_open, candle_close)
            mitigated = _check_mitigation(current_price, zone_low, zone_high)
            if not mitigated:
                bearish_obs.append({
                    "zone_low": zone_low,
                    "zone_high": zone_high,
                    "midpoint": (zone_low + zone_high) / 2,
                    "index": int(i),
                    "mitigated": False,
                })

    logger.debug(
        f"Detected {len(bullish_obs)} bullish OBs, {len(bearish_obs)} bearish OBs"
    )
    return {"bullish": bullish_obs, "bearish": bearish_obs}


def is_price_in_ob(
    price: float,
    ob_zone: dict,
    tolerance: float = 0.005,
) -> bool:
    """
    Check if price is within or very close to an order block zone.

    Args:
        price: Current price
        ob_zone: OB dict with zone_low and zone_high
        tolerance: Additional ± tolerance as fraction of zone width

    Returns:
        True if price is in or near the OB zone
    """
    zone_width = ob_zone["zone_high"] - ob_zone["zone_low"]
    buffer = zone_width * tolerance + ob_zone["zone_low"] * tolerance
    return (ob_zone["zone_low"] - buffer) <= price <= (ob_zone["zone_high"] + buffer)
