"""
Entry engine module - the brain of the SMC Signal Bot.
Orchestrates all SMC detectors, scores confluence, and generates trading setups.

Multi-timeframe flow:
1. Determine bias — all htf_tfs must align
2. Find entry areas (OBs + FVGs on ob_tf)
3. Score confluence (1-5 points)
4. Calculate Entry / SL / TP
5. Validate RR
"""

import pandas as pd
from datetime import datetime
import pytz

from core import data_fetcher as df_module
from core import market_structure, order_blocks, fvg, liquidity, premium_discount, risk_manager
from config.settings import (
    OB_BUFFER_PCT,
    TIMEZONE,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_WIB = pytz.timezone(TIMEZONE)

# Default strategy (swing) — used when no strategy is passed (keeps tests working)
_DEFAULT_STRATEGY = {
    "name": "swing",
    "label": "Swing Trading",
    "emoji": "📈",
    "htf_tfs": ["1d", "4h", "1h"],
    "ob_tf": "4h",
    "entry_tf": "15m",
    "min_confidence": 3,
    "min_rr": 2.0,
    "ob_fresh_lookback": 50,
}


def _find_tp_levels(
    structure_htf: dict,
    structure_4h: dict,
    current_price: float,
    direction: str,
    sl_dist: float = 0.0,
) -> tuple[float | None, float | None]:
    """
    Find TP1 and TP2 from swing structure.

    TP1 = nearest swing level in direction of trade
    TP2 = next swing level, capped at 6× SL distance (realistic for 8-24h horizon)

    With 10x leverage, 6× SL distance = 60× return vs SL — still a meaningful target.
    This prevents TP2 from being set at unreachable historical ATH levels.
    """
    # Cap: TP must be within 6× SL distance from entry
    # If sl_dist not provided, fallback to 3% of price
    sl_dist = sl_dist if sl_dist > 0 else current_price * 0.03
    max_move = 6.0 * sl_dist

    if direction == "bullish":
        tp_cap = current_price + max_move
        candidates = []
        for sh in structure_4h.get("all_swing_highs", []):
            if current_price < sh <= tp_cap:
                candidates.append(sh)
        for sh in structure_htf.get("all_swing_highs", []):
            if current_price < sh <= tp_cap:
                candidates.append(sh)

        candidates = sorted(set(candidates))
        if len(candidates) >= 2:
            tp1 = candidates[0]
            tp2 = candidates[1]  # second nearest (not furthest)
            return tp1, tp2
        elif len(candidates) == 1:
            tp1 = candidates[0]
            tp2 = min(tp1 + (tp1 - current_price), tp_cap)  # extend by TP1 distance
            return tp1, tp2
        else:
            # No structural level in range — use 1.5× and 3× SL as fallback
            return current_price + 1.5 * sl_dist, current_price + 3.0 * sl_dist

    else:  # bearish
        tp_cap = current_price - max_move
        candidates = []
        for sl_lvl in structure_4h.get("all_swing_lows", []):
            if tp_cap <= sl_lvl < current_price:
                candidates.append(sl_lvl)
        for sl_lvl in structure_htf.get("all_swing_lows", []):
            if tp_cap <= sl_lvl < current_price:
                candidates.append(sl_lvl)

        candidates = sorted(set(candidates), reverse=True)
        if len(candidates) >= 2:
            tp1 = candidates[0]
            tp2 = candidates[1]  # second nearest (not furthest)
            return tp1, tp2
        elif len(candidates) == 1:
            tp1 = candidates[0]
            tp2 = max(tp1 - (current_price - tp1), tp_cap)
            return tp1, tp2
        else:
            return current_price - 1.5 * sl_dist, current_price - 3.0 * sl_dist


def _build_reasons(
    direction: str,
    htf_trend: str,
    htf_tfs: list[str],
    ob_tf: str,
    pd_result: dict,
    ob_match: dict | None,
    fvg_overlap: bool,
    liq_near: bool,
    liq_target: dict | None,
) -> list[str]:
    """Build human-readable reason bullets for the alert message."""
    reasons = []
    trend_label = "Bullish" if htf_trend == "bullish" else "Bearish"
    tf_str = " & ".join(tf.upper() for tf in htf_tfs)
    reasons.append(f"TF alignment: Trend {trend_label} di {tf_str}")

    zone_label = "Discount Zone" if pd_result["zone"] == "discount" else "Premium Zone"
    reasons.append(f"Harga di {zone_label} ({pd_result['position_pct']:.1f}% dari range)")

    if ob_match:
        ob_dir = "Bullish" if direction == "bullish" else "Bearish"
        reasons.append(f"Harga masuk ke {ob_dir} Order Block {ob_tf.upper()}")

    if fvg_overlap:
        reasons.append(f"FVG overlap dengan Order Block (high confluence)")

    if liq_near and liq_target:
        reasons.append(f"{liq_target['type']} nearby sebagai liquidity target")

    return reasons


def analyze_pair(
    symbol: str,
    mtf_data: dict[str, pd.DataFrame | None],
    strategy: dict | None = None,
) -> list[dict]:
    """
    Run full SMC multi-timeframe analysis and return valid trading setups.

    Args:
        symbol: Display symbol (e.g. 'BTC/USDT')
        mtf_data: Dict of DataFrames keyed by timeframe
        strategy: Strategy config dict from settings.STRATEGIES.
                  Defaults to swing strategy if None.

    Returns:
        List of setup dicts (empty if no valid setup found).
    """
    if strategy is None:
        strategy = _DEFAULT_STRATEGY

    htf_tfs: list[str] = strategy["htf_tfs"]
    ob_tf: str = strategy["ob_tf"]
    entry_tf: str = strategy["entry_tf"]
    min_confidence: int = strategy["min_confidence"]
    min_rr: float = strategy["min_rr"]
    ob_fresh_lookback: int = strategy["ob_fresh_lookback"]

    # ── 1. Validate all needed TFs ────────────────────────────────────────────
    needed_tfs = list(dict.fromkeys(htf_tfs + [ob_tf]))  # deduplicated, ordered
    for tf in needed_tfs:
        if mtf_data.get(tf) is None or len(mtf_data[tf]) < 10:
            logger.warning(f"{symbol}: Missing or insufficient data for {tf}")
            return []

    df_ob = mtf_data[ob_tf]
    current_price = float(df_ob["close"].iloc[-1])
    logger.info(f"[{strategy['name'].upper()}] Analyzing {symbol} @ {current_price:.4f}")

    # ── 2. Market structure on all needed TFs ─────────────────────────────────
    structs = {tf: market_structure.analyze(mtf_data[tf]) for tf in needed_tfs}
    trends = {tf: structs[tf]["trend"] for tf in htf_tfs}

    # ── 3. Check N-way TF alignment ───────────────────────────────────────────
    top_tf = htf_tfs[0]
    top_trend = trends[top_tf]
    is_ranging_market = False

    # ── 4. Premium / Discount zone from ob_tf (needed for ranging direction) ──
    struct_ob = structs[ob_tf]
    sh_ob = struct_ob.get("last_swing_high")
    sl_ob = struct_ob.get("last_swing_low")

    if sh_ob is None or sl_ob is None:
        logger.info(f"{symbol}: No swing levels on {ob_tf} — skipping")
        return []

    pd_result = premium_discount.calculate(sh_ob, sl_ob, current_price)

    if top_trend == "ranging":
        # Range trading mode: use price position in range to determine bias.
        # Trade at bottom 40% (long) or top 40% (short) — dead zone 40-60%.
        pos_temp = pd_result["position_pct"]
        if pos_temp < 40.0:
            htf_direction = "bullish"
            is_ranging_market = True
            logger.info(f"{symbol}: Ranging {top_tf}, price at range bottom ({pos_temp:.0f}%) — trying LONG")
        elif pos_temp > 60.0:
            htf_direction = "bearish"
            is_ranging_market = True
            logger.info(f"{symbol}: Ranging {top_tf}, price at range top ({pos_temp:.0f}%) — trying SHORT")
        else:
            logger.info(f"{symbol}: Dominant TF {top_tf} is ranging, price at midpoint ({pos_temp:.0f}%) — skipping")
            return []
    else:
        htf_direction = top_trend

        # Allow up to 1 conflicting lower TF (1 conflict = acceptable divergence)
        trend_desc = ", ".join(f"{tf}={t}" for tf, t in trends.items())
        conflicts = sum(
            1 for tf in htf_tfs[1:]
            if trends[tf] != "ranging" and trends[tf] != htf_direction
        )
        if conflicts > 1:
            logger.info(f"{symbol}: TF multi-conflict ({trend_desc}) — skipping")
            return []

        # Require at least (N-2) TFs to actively confirm (ranging TFs don't count)
        confirmations = sum(1 for t in trends.values() if t == htf_direction)
        min_confirmations = max(1, len(htf_tfs) - 2)
        if confirmations < min_confirmations:
            logger.info(f"{symbol}: Weak TF alignment ({trend_desc}, {confirmations}/{len(htf_tfs)} confirm) — skipping")
            return []

    # ── 4b. Zone sanity check (non-ranging path only) ─────────────────────────
    # If price is way outside the detected swing range (pos > 110% or pos < -10%),
    # the swing levels are likely stale — skip to avoid chasing
    if not is_ranging_market:
        pos_sanity = pd_result["position_pct"]
        if htf_direction == "bullish" and pos_sanity > 110.0:
            logger.info(f"{symbol}: Zone {pos_sanity:.0f}% too far above range for LONG — skipping")
            return []
        if htf_direction == "bearish" and pos_sanity < -10.0:
            logger.info(f"{symbol}: Zone {pos_sanity:.0f}% too far below range for SHORT — skipping")
            return []

    # ── 5. ATR + OBs + FVGs + Liquidity on ob_tf ─────────────────────────────
    atr_ob = df_module.compute_atr(df_ob)
    obs_ob = order_blocks.detect(df_ob, atr_ob, struct_ob)
    fvgs_ob = fvg.detect(df_ob)
    liq_ob = liquidity.analyze(df_ob, struct_ob["swing_highs"], struct_ob["swing_lows"])

    # ── 6. TP reference: highest HTF + ob_tf ─────────────────────────────────
    struct_htf_top = structs[htf_tfs[0]]

    # ── 7. Build setup ────────────────────────────────────────────────────────
    setups = []
    is_long = htf_direction == "bullish"
    ob_list = obs_ob["bullish"] if is_long else obs_ob["bearish"]
    fvg_list = fvgs_ob["bullish"] if is_long else fvgs_ob["bearish"]

    # Find matching OB: inside zone OR approaching within 1% — freshness filter
    ob_match = None
    for ob in ob_list:
        ob_age = len(df_ob) - 1 - ob.get("index", 0)
        if ob_age > ob_fresh_lookback:
            continue
        if order_blocks.is_price_in_ob(current_price, ob, tolerance=0.01):
            ob_match = ob
            break

    # Find matching FVG: price inside or within 2% of zone width
    fvg_match = None
    for fv in fvg_list:
        fvg_tol = (fv["zone_high"] - fv["zone_low"]) * 0.02
        if fv["zone_low"] - fvg_tol <= current_price <= fv["zone_high"] + fvg_tol:
            fvg_match = fv
            break

    # ── 8. Confluence scoring (0–6) ───────────────────────────────────────────
    score = 0
    score += 1  # TF alignment confirmed

    # Zone bonus: long needs price in bottom 45%, short in top 45%
    pos = pd_result["position_pct"]
    if is_long and pos < 45.0:
        score += 1
    elif not is_long and pos > 55.0:
        score += 1

    if ob_match is not None:
        score += 1

    if fvg_match is not None:
        score += 1

    fvg_overlap = False
    if ob_match and fvg_match:
        ob_zone = (ob_match["zone_low"], ob_match["zone_high"])
        fv_zone = (fvg_match["zone_low"], fvg_match["zone_high"])
        fvg_overlap = fvg.zones_overlap(ob_zone, fv_zone)
        if fvg_overlap:
            score += 1

    liq_near, liq_target = liquidity.is_near_liquidity(current_price, liq_ob, htf_direction)
    if liq_near:
        score += 1

    # BOS on ob_tf confirms momentum in trade direction
    bos_ob = struct_ob.get("last_bos")
    if bos_ob and bos_ob.get("direction") == htf_direction:
        score += 1

    # CHoCH on entry_tf = early reversal signal (price starting to reverse on lower TF)
    # Checks entry_tf (e.g. 15m/5m) for first sign of smart money reversal in trade direction
    df_entry = mtf_data.get(entry_tf)
    struct_entry = None
    choch_entry_hit = False
    if df_entry is not None and len(df_entry) >= 10:
        struct_entry = market_structure.analyze(df_entry)
        choch_entry = struct_entry.get("last_choch")
        if choch_entry and choch_entry.get("direction") == htf_direction:
            score += 1
            choch_entry_hit = True
    logger.info(
        f"[{strategy['name'].upper()}] {symbol} {htf_direction.upper()}: "
        f"score={score}/8, ob={'yes' if ob_match else 'no'}, "
        f"fvg={'yes' if fvg_match else 'no'}, zone={pd_result['zone']}({pos:.0f}%), "
        f"liq={liq_near}, "
        f"bos={'yes' if bos_ob and bos_ob.get('direction')==htf_direction else 'no'}, "
        f"choch_entry={'yes' if choch_entry_hit else 'no'}"
    )

    if score < min_confidence:
        logger.info(f"{symbol}: Score {score} < {min_confidence} — skipping")
        return setups

    # All strategies require OB or FVG as a structural entry zone
    if ob_match is None and fvg_match is None:
        logger.info(f"{symbol}: Requires OB or FVG — none found, skipping")
        return setups

    # ── 9. Entry / SL / TP ───────────────────────────────────────────────────
    # Entry zone uses CE (Consequent Encroachment) — the half of the OB/FVG
    # that price enters first, making the displayed range tighter and more precise.
    # LONG: price drops into OB from above → upper half (midpoint → zone_high)
    # SHORT: price rises into OB from below → lower half (zone_low → midpoint)
    # SL is still calculated from the full zone edges (unchanged).
    if ob_match:
        entry = ob_match["midpoint"]
        entry_low = ob_match["midpoint"] if is_long else ob_match["zone_low"]
        entry_high = ob_match["zone_high"] if is_long else ob_match["midpoint"]
        _sl_zone_low = ob_match["zone_low"]
        _sl_zone_high = ob_match["zone_high"]
    elif fvg_match:
        entry = fvg_match["midpoint"]
        entry_low = fvg_match["midpoint"] if is_long else fvg_match["zone_low"]
        entry_high = fvg_match["zone_high"] if is_long else fvg_match["midpoint"]
        _sl_zone_low = fvg_match["zone_low"]
        _sl_zone_high = fvg_match["zone_high"]
    else:
        entry = current_price
        entry_low = current_price * (1 - 0.003)
        entry_high = current_price * (1 + 0.003)
        _sl_zone_low = entry_low
        _sl_zone_high = entry_high

    ob_sl_buffer = strategy.get("ob_sl_buffer", OB_BUFFER_PCT)
    if ob_match:
        sl = ob_match["zone_low"] * (1 - ob_sl_buffer) if is_long else ob_match["zone_high"] * (1 + ob_sl_buffer)
    elif fvg_match:
        # FVG SL: place outside the FVG zone + buffer (same logic as OB)
        # Also enforce minimum 1.5×ATR so SL is never too tight
        atr_val = float(atr_ob.iloc[-1]) if not atr_ob.empty and not pd.isna(atr_ob.iloc[-1]) else entry * 0.015
        fvg_sl = fvg_match["zone_low"] * (1 - ob_sl_buffer) if is_long else fvg_match["zone_high"] * (1 + ob_sl_buffer)
        atr_sl = entry - 1.5 * atr_val if is_long else entry + 1.5 * atr_val
        # Take the wider of the two (more conservative SL)
        sl = min(fvg_sl, atr_sl) if is_long else max(fvg_sl, atr_sl)
    else:
        # No structural zone — use 1.5×ATR with minimum 0.5% of price
        atr_val = float(atr_ob.iloc[-1]) if not atr_ob.empty and not pd.isna(atr_ob.iloc[-1]) else entry * 0.015
        min_sl_dist = entry * 0.005
        sl_dist = max(1.5 * atr_val, min_sl_dist)
        sl = entry - sl_dist if is_long else entry + sl_dist

    # TP reference = outer edge of entry zone so TP is always outside the zone
    tp_reference = entry_high if is_long else entry_low
    sl_dist = abs(entry - sl)
    tp1, tp2 = _find_tp_levels(struct_htf_top, struct_ob, tp_reference, htf_direction, sl_dist)
    if tp1 is None or tp2 is None:
        logger.info(f"{symbol}: Could not determine TP levels — skipping")
        return setups

    # Cap TPs by strategy time horizon — use current_price as basis (NOT entry_high/low
    # which can be a historical OB boundary from months ago, causing TP2 = 14.39 on LINK)
    # swing (24h): max 8%  |  intraday (8h): max 4%  |  scalp: max 1.5%
    _MAX_TP_PCT = {"swing": 0.08, "intraday": 0.04, "scalp": 0.015}
    max_tp_pct = _MAX_TP_PCT.get(strategy.get("name", "swing"), 0.08)
    if is_long:
        tp1 = min(tp1, current_price * (1 + max_tp_pct * 0.4))
        tp2 = min(tp2, current_price * (1 + max_tp_pct))
    else:
        tp1 = max(tp1, current_price * (1 - max_tp_pct * 0.4))
        tp2 = max(tp2, current_price * (1 - max_tp_pct))

    # ── 10. Validate SL position and distance ────────────────────────────────
    # SL must be OUTSIDE the full zone (use _sl_zone, not CE entry_low/high)
    if is_long and sl >= _sl_zone_low:
        logger.info(f"{symbol}: SL {sl:.6f} inside full zone [{_sl_zone_low:.6f}-{_sl_zone_high:.6f}] — fixing")
        sl = _sl_zone_low * (1 - strategy.get("ob_sl_buffer", OB_BUFFER_PCT))
    elif not is_long and sl <= _sl_zone_high:
        logger.info(f"{symbol}: SL {sl:.6f} inside full zone [{_sl_zone_low:.6f}-{_sl_zone_high:.6f}] — fixing")
        sl = _sl_zone_high * (1 + strategy.get("ob_sl_buffer", OB_BUFFER_PCT))

    # SL must meet minimum distance from entry
    sl_dist_pct = abs(entry - sl) / entry
    min_sl_pct = strategy.get("min_sl_pct", 0.005)  # default 0.5%
    if sl_dist_pct < min_sl_pct:
        logger.info(
            f"{symbol}: SL too tight ({sl_dist_pct*100:.2f}% < {min_sl_pct*100:.1f}%) — skipping"
        )
        return setups

    # ── 11. Validate RR ──────────────────────────────────────────────────────
    is_valid, rr_tp1, rr_tp2 = risk_manager.validate_setup(
        entry, sl, tp1, tp2, htf_direction, min_rr=min_rr
    )
    if not is_valid:
        logger.info(f"{symbol}: RR {rr_tp2:.2f} < {min_rr} — skipping")
        return setups

    # ── 11. Build reasons + setup dict ───────────────────────────────────────
    reasons = _build_reasons(
        htf_direction, htf_direction, htf_tfs, ob_tf,
        pd_result, ob_match, fvg_overlap, liq_near, liq_target
    )

    htf_trend_str = " | ".join(
        f"{tf.upper()}: {trends[tf].capitalize()}" for tf in htf_tfs
    )
    if is_ranging_market:
        htf_trend_str = f"RANGE TRADE | {htf_trend_str}"
    # Entry TF display: "4H OB → 15m entry" style
    timeframe_entry_label = f"{ob_tf.upper()} OB → {entry_tf} konfirmasi"

    now_wib = datetime.now(tz=_WIB)
    setup = {
        "symbol": symbol,
        "direction": "LONG" if is_long else "SHORT",
        "entry_low": round(entry_low, 6),
        "entry_high": round(entry_high, 6),
        "entry_mid": round(entry, 6),
        "sl": round(sl, 6),
        "tp1": round(tp1, 6),
        "tp2": round(tp2, 6),
        "rr_tp1": rr_tp1,
        "rr_tp2": rr_tp2,
        "confidence": score,
        "htf_trend": htf_trend_str,
        "reasons": reasons,
        "timeframe_entry": timeframe_entry_label,
        "timestamp": now_wib,
        "current_price": current_price,
        "strategy_name": strategy["name"],
        "strategy_label": strategy["label"],
        "strategy_emoji": strategy["emoji"],
        "entry_tf": entry_tf,
    }

    setups.append(setup)
    logger.info(
        f"[{strategy['name'].upper()}] Valid setup: {symbol} {htf_direction.upper()} "
        f"entry={entry:.4f} sl={sl:.4f} tp2={tp2:.4f} rr={rr_tp2:.2f}"
    )
    return setups
