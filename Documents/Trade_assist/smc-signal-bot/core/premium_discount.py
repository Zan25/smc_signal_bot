"""
Premium & Discount zone calculation module.
Calculates the 50% equilibrium level between swing high and swing low.

Premium zone (above 50%): ideal for SELL/SHORT setups
Discount zone (below 50%): ideal for BUY/LONG setups
"""

from utils.logger import get_logger

logger = get_logger(__name__)


def calculate(
    swing_high: float,
    swing_low: float,
    current_price: float,
) -> dict:
    """
    Calculate the premium/discount zone classification for a price.

    Args:
        swing_high: Most recent confirmed swing high price
        swing_low: Most recent confirmed swing low price
        current_price: Current market price

    Returns:
        Dict with:
            swing_high (float)
            swing_low (float)
            equilibrium (float): 50% level
            zone (str): 'premium', 'discount', or 'equilibrium'
            position_pct (float): 0-100 scale where 50 = equilibrium
    """
    if swing_high <= swing_low:
        logger.warning(f"Invalid swing levels: high={swing_high}, low={swing_low}")
        return {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "equilibrium": (swing_high + swing_low) / 2,
            "zone": "equilibrium",
            "position_pct": 50.0,
        }

    range_size = swing_high - swing_low
    equilibrium = swing_low + range_size * 0.5

    # Position as percentage of range (0 = at swing low, 100 = at swing high)
    position_pct = ((current_price - swing_low) / range_size) * 100

    if current_price > equilibrium:
        zone = "premium"
    elif current_price < equilibrium:
        zone = "discount"
    else:
        zone = "equilibrium"

    logger.debug(
        f"Premium/Discount: price={current_price:.4f}, eq={equilibrium:.4f}, "
        f"zone={zone}, pos={position_pct:.1f}%"
    )

    return {
        "swing_high": swing_high,
        "swing_low": swing_low,
        "equilibrium": equilibrium,
        "zone": zone,
        "position_pct": round(position_pct, 2),
    }
