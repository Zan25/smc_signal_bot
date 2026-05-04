"""
Weighted confluence scoring (v2) — alternative to flat 1-point-per-factor used in entry_engine.

Activated by setting `scoring_version: "v2"` in a strategy dict.

Rationale (validated by backtest analysis):
  - BOS / CHoCH at entry_tf are momentum confirmations → high WR contribution → weight 2
  - Flag pattern is a structural continuation pattern → weight 2
  - OB+FVG overlap (high confluence) → weight 2
  - All passive location factors (zone, liq, OB-only, FVG-only) → weight 1

Max possible score: 13 (vs 9 in v1).
Recommended thresholds:
  swing:    min_confidence = 7
  intraday: min_confidence = 6
"""


def compute_score(
    is_long: bool,
    pos_pct: float,
    ob_match: dict | None,
    fvg_match: dict | None,
    fvg_overlap: bool,
    liq_near: bool,
    bos_aligned: bool,
    choch_entry_aligned: bool,
    flag_info: dict | None,
) -> tuple[int, dict]:
    """
    Returns (total_score, components_dict).
    components_dict tracks which factors contributed (for analysis/debug).
    """
    components = {}

    # Always-on: TF alignment confirmed by analyze_pair before reaching scoring
    score = 1
    components["tf_align"] = 1

    # Zone bonus — passive location, weight 1
    if (is_long and pos_pct < 50.0) or (not is_long and pos_pct > 50.0):
        score += 1
        components["zone"] = 1

    # OB at zone — passive location, weight 1
    if ob_match is not None:
        score += 1
        components["ob"] = 1

    # FVG at zone — passive location, weight 1
    if fvg_match is not None:
        score += 1
        components["fvg"] = 1

    # OB+FVG overlap — high confluence, weight 2 (was 1)
    if fvg_overlap:
        score += 2
        components["ob_fvg_overlap"] = 2

    # Liquidity nearby — weight 1
    if liq_near:
        score += 1
        components["liq"] = 1

    # BOS aligned — momentum confirmation, weight 2 (was 1)
    if bos_aligned:
        score += 2
        components["bos"] = 2

    # CHoCH at entry_tf aligned — early reversal sign, weight 2 (was 1)
    if choch_entry_aligned:
        score += 2
        components["choch_entry"] = 2

    # Flag pattern — strong continuation, weight 2 (was 1)
    if flag_info:
        score += 2
        components["flag"] = 2

    return score, components


def is_in_skip_session_utc(timestamp_utc, skip_start_hour: int, skip_end_hour: int) -> bool:
    """
    Check if UTC timestamp falls in skip session window (e.g. 17-23 UTC = 00-06 WIB Asia low-liq).

    Args:
        timestamp_utc: datetime in UTC
        skip_start_hour: e.g. 17 (UTC) = 00 WIB
        skip_end_hour: e.g. 23 (UTC) = 06 WIB

    Returns:
        True if hour falls in [skip_start, skip_end) — should skip signal.
    """
    h = timestamp_utc.hour
    if skip_start_hour <= skip_end_hour:
        return skip_start_hour <= h < skip_end_hour
    # Wraps around midnight, e.g. 22..04
    return h >= skip_start_hour or h < skip_end_hour
