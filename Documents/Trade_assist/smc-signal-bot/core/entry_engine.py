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
    "min_confidence": 4,
    "min_rr": 3.0,
    "ob_fresh_lookback": 50,
}


def _find_tp_levels(
    structure_htf: dict,
    structure_4h: dict,
    current_price: float,
    direction: str,
) -> tuple[float | None, float | None]:
    """
    Find TP1 and TP2 from swing structure.

    TP1 = nearest swing level in direction of trade
    TP2 = further swing level (major HTF swing high/low)
    """
    if direction == "bullish":
        # Look for resistance levels above current price
        candidates = []
        for sh in structure_4h.get("all_swing_highs", []):
            if sh > current_price:
                candidates.append(sh)
        for sh in structure_htf.get("all_swing_highs", []):
            if sh > current_price:
                candidates.append(sh)

        candidates = sorted(set(candidates))
        if len(candidates) >= 2:
            return candidates[0], candidates[-1]
        elif len(candidates) == 1:
            return candidates[0], candidates[0] * 1.03  # fallback: 3% above
        else:
            return current_price * 1.02, current_price * 1.05

    else:  # bearish
        # Look for support levels below current price
        candidates = []
        for sl in structure_4h.get("all_swing_lows", []):
            if sl < current_price:
                candidates.append(sl)
        for sl in structure_htf.get("all_swing_lows", []):
            if sl < current_price:
                candidates.append(sl)

        candidates = sorted(set(candidates), reverse=True)
        if len(candidates) >= 2:
            return candidates[0], candidates[-1]
        elif len(candidates) == 1:
            return candidates[0], candidates[0] * 0.97
        else:
            return current_price * 0.98, current_price * 0.95


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
    # Rule: dominant HTF (highest TF) must have a clear bias (not ranging).
    # Lower TFs may be ranging (consolidation) but must NOT be opposite to bias.
    # Minimum 2 of N TFs must actively confirm the direction.
    top_tf = htf_tfs[0]
    top_trend = trends[top_tf]

    if top_trend == "ranging":
        logger.info(f"{symbol}: Dominant TF {top_tf} is ranging — skipping")
        return []

    htf_direction = top_trend

    # Reject if any lower TF is opposite to bias
    for tf in htf_tfs[1:]:
        if trends[tf] != "ranging" and trends[tf] != htf_direction:
            trend_desc = ", ".join(f"{tf}={t}" for tf, t in trends.items())
            logger.info(f"{symbol}: TF conflict ({trend_desc}) — skipping")
            return []

    # Require at least (N-1) TFs to actively confirm (ranging TFs don't count)
    confirmations = sum(1 for t in trends.values() if t == htf_direction)
    min_confirmations = max(1, len(htf_tfs) - 1)
    if confirmations < min_confirmations:
        trend_desc = ", ".join(f"{tf}={t}" for tf, t in trends.items())
        logger.info(f"{symbol}: Weak TF alignment ({trend_desc}, {confirmations}/{len(htf_tfs)} confirm) — skipping")
        return []

    # ── 4. Premium / Discount zone from ob_tf ────────────────────────────────
    struct_ob = structs[ob_tf]
    sh_ob = struct_ob.get("last_swing_high")
    sl_ob = struct_ob.get("last_swing_low")

    if sh_ob is None or sl_ob is None:
        logger.info(f"{symbol}: No swing levels on {ob_tf} — skipping")
        return []

    pd_result = premium_discount.calculate(sh_ob, sl_ob, current_price)

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

    # Find matching OB: strictly inside zone + freshness filter
    ob_match = None
    for ob in ob_list:
        ob_age = len(df_ob) - 1 - ob.get("index", 0)
        if ob_age > ob_fresh_lookback:
            continue
        if ob["zone_low"] <= current_price <= ob["zone_high"]:
            ob_match = ob
            break

    # Find matching FVG: price strictly inside zone
    fvg_match = None
    for fv in fvg_list:
        if fv["zone_low"] <= current_price <= fv["zone_high"]:
            fvg_match = fv
            break

    # ── 8. Confluence scoring (0–6) ───────────────────────────────────────────
    score = 0
    score += 1  # TF alignment confirmed

    # Deep zone only: long needs price in bottom 30%, short in top 30%
    pos = pd_result["position_pct"]
    if is_long and pos < 30.0:
        score += 1
    elif not is_long and pos > 70.0:
        score += 1

    if ob_match is not None:
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

    # CHoCH on ob_tf = early reversal signal in trade direction (first sign of smart money entry)
    choch_ob = struct_ob.get("last_choch")
    if choch_ob and choch_ob.get("direction") == htf_direction:
        score += 1

    logger.info(
        f"[{strategy['name'].upper()}] {symbol} {htf_direction.upper()}: "
        f"score={score}/7, ob={'yes' if ob_match else 'no'}, "
        f"fvg={'yes' if fvg_match else 'no'}, zone={pd_result['zone']}({pos:.0f}%), "
        f"liq={liq_near}, "
        f"bos={'yes' if bos_ob and bos_ob.get('direction')==htf_direction else 'no'}, "
        f"choch={'yes' if choch_ob and choch_ob.get('direction')==htf_direction else 'no'}"
    )

    if score < min_confidence:
        logger.info(f"{symbol}: Score {score} < {min_confidence} — skipping")
        return setups

    # Scalp & intraday require OB — no OB = no precise entry zone
    if strategy["name"] in ("scalp", "intraday") and ob_match is None:
        logger.info(f"{symbol}: {strategy['name'].capitalize()} requires OB — none found, skipping")
        return setups

    # Swing requires at least OB or FVG
    if strategy["name"] == "swing" and ob_match is None and fvg_match is None:
        logger.info(f"{symbol}: Swing requires OB or FVG — none found, skipping")
        return setups

    # ── 9. Entry / SL / TP ───────────────────────────────────────────────────
    if ob_match:
        entry = ob_match["midpoint"]
        entry_low = ob_match["zone_low"]
        entry_high = ob_match["zone_high"]
    elif fvg_match:
        entry = fvg_match["midpoint"]
        entry_low = fvg_match["zone_low"]
        entry_high = fvg_match["zone_high"]
    else:
        entry = current_price
        entry_low = current_price * (1 - 0.003)
        entry_high = current_price * (1 + 0.003)

    ob_sl_buffer = strategy.get("ob_sl_buffer", OB_BUFFER_PCT)
    if ob_match:
        sl = ob_match["zone_low"] * (1 - ob_sl_buffer) if is_long else ob_match["zone_high"] * (1 + ob_sl_buffer)
    else:
        # Use 1×ATR for SL — adapts to market volatility instead of fixed %
        atr_val = float(atr_ob.iloc[-1]) if not atr_ob.empty and not pd.isna(atr_ob.iloc[-1]) else entry * 0.015
        sl = entry - 1.5 * atr_val if is_long else entry + 1.5 * atr_val

    # TP reference = outer edge of entry zone so TP is always outside the zone
    tp_reference = entry_high if is_long else entry_low
    tp1, tp2 = _find_tp_levels(struct_htf_top, struct_ob, tp_reference, htf_direction)
    if tp1 is None or tp2 is None:
        logger.info(f"{symbol}: Could not determine TP levels — skipping")
        return setups

    # ── 10. Validate RR ──────────────────────────────────────────────────────
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
