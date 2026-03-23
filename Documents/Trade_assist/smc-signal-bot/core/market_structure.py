"""
Market structure detection module.
Detects swing highs/lows using fractal method, determines trend,
and identifies Break of Structure (BOS) and Change of Character (CHoCH).
"""

import pandas as pd
import numpy as np
from utils.logger import get_logger
from config.settings import SWING_LOOKBACK

logger = get_logger(__name__)


def detect_swings(df: pd.DataFrame, n: int = SWING_LOOKBACK) -> tuple[pd.Series, pd.Series]:
    """
    Detect swing highs and lows using the fractal method.

    A swing high at index i requires: high[i] == max(high[i-n : i+n+1])
    A swing low at index i requires: low[i] == min(low[i-n : i+n+1])

    Note: The last n candles cannot have confirmed swings (they need future candles).

    Args:
        df: OHLCV DataFrame
        n: Number of candles on each side required for confirmation (default 2)

    Returns:
        (swing_highs, swing_lows) - pd.Series with NaN where no swing, price where swing exists
    """
    highs = df["high"].values
    lows = df["low"].values
    size = len(df)

    swing_h = np.full(size, np.nan)
    swing_l = np.full(size, np.nan)

    for i in range(n, size - n):
        window_h = highs[i - n: i + n + 1]
        window_max = window_h.max()
        if highs[i] >= window_max * (1 - 1e-5):
            swing_h[i] = highs[i]

        window_l = lows[i - n: i + n + 1]
        window_min = window_l.min()
        if lows[i] <= window_min * (1 + 1e-5):
            swing_l[i] = lows[i]

    return pd.Series(swing_h, index=df.index), pd.Series(swing_l, index=df.index)


def _get_confirmed_swings(swing_series: pd.Series, last_n: int = 5) -> list[float]:
    """Extract last N non-NaN swing values from a series."""
    values = swing_series.dropna().values
    return list(values[-last_n:]) if len(values) > 0 else []


def determine_trend(swing_highs: pd.Series, swing_lows: pd.Series) -> str:
    """
    Determine market trend using weighted scoring on recent swings.

    Recent swings carry more weight than older ones. A clear directional
    majority (> 1 point difference) is required to declare a trend.

    Bullish signal: Higher High (HH) or Higher Low (HL)
    Bearish signal: Lower High (LH) or Lower Low (LL)

    Args:
        swing_highs: Series of confirmed swing highs
        swing_lows: Series of confirmed swing lows

    Returns:
        'bullish', 'bearish', or 'ranging'
    """
    highs = _get_confirmed_swings(swing_highs, last_n=4)
    lows = _get_confirmed_swings(swing_lows, last_n=4)

    if len(highs) < 2 and len(lows) < 2:
        return "ranging"

    bull_score = 0.0
    bear_score = 0.0

    # Weight recent swings more heavily (weight = index position)
    for i in range(1, len(highs)):
        weight = float(i)
        if highs[i] > highs[i - 1]:
            bull_score += weight
        elif highs[i] < highs[i - 1]:
            bear_score += weight

    for i in range(1, len(lows)):
        weight = float(i)
        if lows[i] > lows[i - 1]:
            bull_score += weight
        elif lows[i] < lows[i - 1]:
            bear_score += weight

    if bull_score > bear_score + 0.5:
        return "bullish"
    elif bear_score > bull_score + 0.5:
        return "bearish"
    return "ranging"


def detect_bos(
    df: pd.DataFrame,
    swing_highs: pd.Series,
    swing_lows: pd.Series,
    trend: str,
) -> dict | None:
    """
    Detect the most recent Break of Structure (BOS).

    BOS confirms trend continuation:
    - Bullish BOS: current close > last confirmed swing high (in uptrend)
    - Bearish BOS: current close < last confirmed swing low (in downtrend)

    Args:
        df: OHLCV DataFrame
        swing_highs: Confirmed swing highs series
        swing_lows: Confirmed swing lows series
        trend: Current trend direction

    Returns:
        Dict with BOS details or None
    """
    current_close = float(df["close"].iloc[-1])
    highs = _get_confirmed_swings(swing_highs)
    lows = _get_confirmed_swings(swing_lows)

    if trend == "bullish" and highs:
        last_sh = highs[-1]
        if current_close > last_sh:
            return {
                "type": "BOS",
                "direction": "bullish",
                "level": last_sh,
                "confirmed": True,
            }

    if trend == "bearish" and lows:
        last_sl = lows[-1]
        if current_close < last_sl:
            return {
                "type": "BOS",
                "direction": "bearish",
                "level": last_sl,
                "confirmed": True,
            }

    return None


def detect_choch(
    df: pd.DataFrame,
    swing_highs: pd.Series,
    swing_lows: pd.Series,
    trend: str,
) -> dict | None:
    """
    Detect the most recent Change of Character (CHoCH).

    CHoCH is the first signal that trend may be reversing:
    - Bullish CHoCH: in downtrend, current close breaks above a swing high
    - Bearish CHoCH: in uptrend, current close breaks below a swing low

    Args:
        df: OHLCV DataFrame
        swing_highs: Confirmed swing highs series
        swing_lows: Confirmed swing lows series
        trend: Current trend direction

    Returns:
        Dict with CHoCH details or None
    """
    current_close = float(df["close"].iloc[-1])
    highs = _get_confirmed_swings(swing_highs)
    lows = _get_confirmed_swings(swing_lows)

    if trend == "bearish" and highs:
        last_sh = highs[-1]
        # Require close 0.05% above level to avoid false pin-bar triggers
        if current_close > last_sh * 1.0005:
            return {
                "type": "CHoCH",
                "direction": "bullish",
                "level": last_sh,
            }

    if trend == "bullish" and lows:
        last_sl = lows[-1]
        if current_close < last_sl * 0.9995:
            return {
                "type": "CHoCH",
                "direction": "bearish",
                "level": last_sl,
            }

    return None


def analyze(df: pd.DataFrame, n: int = SWING_LOOKBACK) -> dict:
    """
    Run full market structure analysis on a DataFrame.

    Args:
        df: OHLCV DataFrame (at least 2*n+1 candles)
        n: Fractal swing lookback (default 2)

    Returns:
        Dict with keys:
            trend (str): 'bullish', 'bearish', or 'ranging'
            swing_highs (pd.Series): confirmed swing high prices
            swing_lows (pd.Series): confirmed swing low prices
            last_swing_high (float|None): most recent confirmed swing high price
            last_swing_low (float|None): most recent confirmed swing low price
            last_bos (dict|None): most recent BOS event
            last_choch (dict|None): most recent CHoCH event
    """
    if len(df) < 2 * n + 1:
        logger.warning(f"Insufficient candles ({len(df)}) for swing detection with n={n}")
        return {
            "trend": "ranging",
            "swing_highs": pd.Series(dtype=float),
            "swing_lows": pd.Series(dtype=float),
            "last_swing_high": None,
            "last_swing_low": None,
            "last_bos": None,
            "last_choch": None,
            "flag_pattern": None,
        }

    swing_highs, swing_lows = detect_swings(df, n=n)
    trend = determine_trend(swing_highs, swing_lows)

    sh_vals = _get_confirmed_swings(swing_highs)
    sl_vals = _get_confirmed_swings(swing_lows)

    last_bos = detect_bos(df, swing_highs, swing_lows, trend)
    last_choch = detect_choch(df, swing_highs, swing_lows, trend)

    return {
        "trend": trend,
        "swing_highs": swing_highs,
        "swing_lows": swing_lows,
        "last_swing_high": sh_vals[-1] if sh_vals else None,
        "last_swing_low": sl_vals[-1] if sl_vals else None,
        "all_swing_highs": sh_vals,
        "all_swing_lows": sl_vals,
        "last_bos": last_bos,
        "last_choch": last_choch,
        "flag_pattern": detect_flag_pattern(df, trend),
    }


def detect_flag_pattern(df: pd.DataFrame, trend: str) -> dict | None:
    """
    Detect bear/bull flag: impulsive move followed by tight consolidation.

    Bear flag = bearish trend + recent impulse down (candles -20 to -10)
                + current flag body (last 10 candles) is ≥35% tighter than impulse
    Bull flag = inverse pattern.

    This catches setups like the BTC bear flag visible on the daily chart:
    flagpole (sharp sell-off) + flag (tight consolidation/bounce) = bearish continuation.

    Returns:
        {'type': 'bear_flag'|'bull_flag', 'compression': float} or None
    """
    if len(df) < 30 or trend == "ranging":
        return None

    # Impulse window: candles 20–10 ago (the 'flagpole')
    impulse = df.iloc[-20:-10]
    # Flag window: last 10 candles (the 'flag body')
    flag = df.iloc[-10:]

    impulse_range = float(impulse["high"].max() - impulse["low"].min())
    flag_range = float(flag["high"].max() - flag["low"].min())

    if impulse_range == 0:
        return None

    # Compression ratio: flag range as % of impulse range
    # A true flag has ≥35% tighter range than the impulse (compression < 0.65)
    compression = flag_range / impulse_range
    if compression >= 0.65:
        return None

    # Impulse direction must match trend
    impulse_start = float(impulse["close"].iloc[0])
    impulse_end = float(impulse["close"].iloc[-1])

    if trend == "bearish" and impulse_start > impulse_end:
        return {"type": "bear_flag", "compression": round(compression, 2)}

    if trend == "bullish" and impulse_start < impulse_end:
        return {"type": "bull_flag", "compression": round(compression, 2)}

    return None
