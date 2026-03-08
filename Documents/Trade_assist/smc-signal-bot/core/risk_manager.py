"""
Risk management module.
Handles position sizing, risk-reward calculation, and trade validation.
"""

from utils.logger import get_logger
from config.settings import DEFAULT_CAPITAL, RISK_PER_TRADE_PERCENT, MIN_RR_RATIO

logger = get_logger(__name__)


def calculate_rr(entry: float, sl: float, tp: float) -> float:
    """
    Calculate risk-reward ratio for a trade.

    Args:
        entry: Entry price
        sl: Stop loss price
        tp: Take profit price

    Returns:
        RR ratio as float (e.g. 2.5 means 1:2.5 RR). Returns 0.0 if invalid.
    """
    risk = abs(entry - sl)
    reward = abs(tp - entry)

    if risk == 0:
        logger.warning("RR calculation: risk is zero (entry == sl)")
        return 0.0

    return round(reward / risk, 2)


def calculate_position_size(
    capital: float = DEFAULT_CAPITAL,
    risk_pct: float = RISK_PER_TRADE_PERCENT,
    entry: float = 0.0,
    sl: float = 0.0,
) -> dict:
    """
    Calculate position size based on capital and risk parameters.

    Args:
        capital: Total trading capital in USDT
        risk_pct: Percentage of capital to risk per trade (e.g. 1 = 1%)
        entry: Entry price
        sl: Stop loss price

    Returns:
        Dict with risk_amount, sl_distance_pct, units, notional_value
    """
    if entry <= 0 or sl <= 0:
        return {"risk_amount": 0, "sl_distance_pct": 0, "units": 0, "notional_value": 0}

    risk_amount = capital * (risk_pct / 100)
    sl_distance = abs(entry - sl)
    sl_distance_pct = (sl_distance / entry) * 100

    if sl_distance == 0:
        return {"risk_amount": risk_amount, "sl_distance_pct": 0, "units": 0, "notional_value": 0}

    # Position size = risk_amount / sl_distance_in_price
    units = risk_amount / sl_distance
    notional_value = units * entry

    return {
        "risk_amount": round(risk_amount, 2),
        "sl_distance_pct": round(sl_distance_pct, 4),
        "units": round(units, 6),
        "notional_value": round(notional_value, 2),
    }


def validate_setup(
    entry: float,
    sl: float,
    tp1: float,
    tp2: float,
    direction: str,
    min_rr: float = MIN_RR_RATIO,
) -> tuple[bool, float, float]:
    """
    Validate a trading setup meets minimum risk-reward criteria.

    Args:
        entry: Entry price
        sl: Stop loss price
        tp1: First take profit (partial close)
        tp2: Second take profit (final target)
        direction: 'bullish' (long) or 'bearish' (short)
        min_rr: Minimum acceptable RR ratio (default 2.0 for 1:2)

    Returns:
        (is_valid, rr_to_tp1, rr_to_tp2)
        is_valid = True only if rr_to_tp2 >= min_rr AND direction logic is correct
    """
    # Directional sanity checks
    if direction == "bullish":
        if not (sl < entry < tp1 <= tp2):
            logger.debug(f"Invalid LONG levels: sl={sl} entry={entry} tp1={tp1} tp2={tp2}")
            return False, 0.0, 0.0
    elif direction == "bearish":
        if not (tp2 <= tp1 < entry < sl):
            logger.debug(f"Invalid SHORT levels: sl={sl} entry={entry} tp1={tp1} tp2={tp2}")
            return False, 0.0, 0.0
    else:
        return False, 0.0, 0.0

    rr_tp1 = calculate_rr(entry, sl, tp1)
    rr_tp2 = calculate_rr(entry, sl, tp2)
    is_valid = rr_tp2 >= min_rr

    return is_valid, rr_tp1, rr_tp2
