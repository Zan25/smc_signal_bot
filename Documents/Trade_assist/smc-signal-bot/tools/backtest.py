"""
Walk-forward backtester for SMC entry engine.

For each ob_tf candle in the test period:
  1. Slice all TFs to data ≤ candle close (no lookahead)
  2. Call entry_engine.analyze_pair() with the slice
  3. If signal fires, simulate trade using lower-TF candles after entry
  4. Track outcome (TP1/TP2/SL/EOD/EXPIRED) with realistic fees

Usage:
  python tools/backtest.py [strategy_name] [pairs_csv]
  e.g. python tools/backtest.py intraday BTC,ETH,SOL
"""
import sys
import json
import logging
import warnings
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import pandas as pd
import numpy as np

# Project root on path FIRST — needed before any project imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Suppress logging noise from core modules — backtest does 1000s of scans, INFO logs are noise
warnings.filterwarnings("ignore")

# Patch get_logger BEFORE importing core, so all loggers from core start at ERROR
import utils.logger as _utils_logger
_orig_get_logger = _utils_logger.get_logger
def _quiet_get_logger(name):
    lg = _orig_get_logger(name)
    lg.setLevel(logging.ERROR)
    for h in lg.handlers:
        h.setLevel(logging.ERROR)
    return lg
_utils_logger.get_logger = _quiet_get_logger

# Now safe to import core modules — their loggers will be silenced
from config.settings import STRATEGIES
from core import entry_engine

CACHE_DIR = Path(__file__).resolve().parent.parent / "backtest_data"
RESULTS_DIR = Path(__file__).resolve().parent.parent / "backtest_results"
RESULTS_DIR.mkdir(exist_ok=True)

# Trading cost assumptions (Gate.io perpetual)
TAKER_FEE = 0.0005   # 0.05% per side
SLIPPAGE = 0.0003    # 0.03% slippage on entry/exit
LEVERAGE = 10

# Strategy → entry_tf for trade simulation
ENTRY_TF_FOR_SIM = {
    "swing": "15m",
    "intraday": "5m",
    "scalp": "5m",
}

# Max bars to simulate per trade (capped per strategy)
MAX_SIM_BARS = {
    "swing":    36 * 4,   # 36h × 4 (15m bars)
    "intraday": 12 * 12,  # 12h × 12 (5m bars)
    "scalp":     4 * 12,
}


def load_csv(symbol: str, tf: str) -> pd.DataFrame:
    safe = symbol.replace("/", "_").replace(":", "_")
    path = CACHE_DIR / f"{safe}_{tf}.csv"
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["timestamp"])
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    return df


def slice_up_to(df: pd.DataFrame, ts, ts_arr: np.ndarray | None = None) -> pd.DataFrame:
    """Return rows with timestamp <= ts. Uses precomputed ts_arr (ns int64) for O(log n) lookup."""
    if ts_arr is not None:
        idx = np.searchsorted(ts_arr, np.datetime64(ts).astype("datetime64[ns]").astype("int64"), side="right")
        return df.iloc[:idx].reset_index(drop=True)
    return df[df["timestamp"] <= ts].reset_index(drop=True)


def simulate_trade(
    entry_price: float,
    sl: float,
    tp1: float,
    tp2: float,
    direction: str,
    sim_df: pd.DataFrame,   # entry_tf candles AFTER entry timestamp
    risk_usd: float,
    strategy_name: str,
) -> dict:
    """
    Walk forward through simulation candles checking SL/TP/expiry.

    SL/TP intra-bar resolution rule (worst-case for trader):
      - If both SL and TP hit on same candle, SL wins (conservative).
      - Use high/low to test wicks.
    """
    is_long = direction == "LONG"
    sl_dist_pct = abs(entry_price - sl) / entry_price
    if sl_dist_pct < 1e-6:
        return {"outcome": "INVALID", "pnl_R": 0.0, "bars": 0}

    # Position sizing: risk_usd is fixed; exposure scales accordingly
    exposure = risk_usd / sl_dist_pct
    qty = exposure / entry_price

    max_bars = MAX_SIM_BARS.get(strategy_name, 200)
    sim_df = sim_df.head(max_bars)

    if len(sim_df) == 0:
        return {"outcome": "NO_DATA", "pnl_R": 0.0, "bars": 0}

    # Apply entry slippage
    fill_price = entry_price * (1 + SLIPPAGE) if is_long else entry_price * (1 - SLIPPAGE)
    entry_fee = exposure * TAKER_FEE   # in USD

    tp1_hit = False
    tp1_pnl = 0.0
    tp1_fee = 0.0
    sl_after_tp1 = fill_price  # breakeven after TP1

    for i, row in sim_df.iterrows():
        h, l = float(row["high"]), float(row["low"])

        # Check both SL and TP1/TP2 in same candle
        if not tp1_hit:
            sl_breach = (is_long and l <= sl) or (not is_long and h >= sl)
            tp1_breach = (is_long and h >= tp1) or (not is_long and l <= tp1)
            tp2_breach = (is_long and h >= tp2) or (not is_long and l <= tp2)

            # Conservative tie-breaker: SL wins if both touched in same bar
            if sl_breach and tp1_breach:
                # Single-bar SL hit (no TP1 yet)
                exit_price = sl * (1 - SLIPPAGE) if is_long else sl * (1 + SLIPPAGE)
                pnl_gross = qty * (exit_price - fill_price) if is_long else qty * (fill_price - exit_price)
                exit_fee = exposure * TAKER_FEE
                pnl_net = pnl_gross - entry_fee - exit_fee
                pnl_R = pnl_net / risk_usd
                return {"outcome": "SL", "pnl_R": pnl_R, "bars": i + 1, "exit_price": exit_price}

            if tp2_breach:
                # Skip TP1 partial: full close at TP2 (for cases where price gaps past TP1 to TP2)
                exit_price = tp2 * (1 - SLIPPAGE) if is_long else tp2 * (1 + SLIPPAGE)
                pnl_gross = qty * (exit_price - fill_price) if is_long else qty * (fill_price - exit_price)
                exit_fee = exposure * TAKER_FEE
                pnl_net = pnl_gross - entry_fee - exit_fee
                pnl_R = pnl_net / risk_usd
                return {"outcome": "TP2", "pnl_R": pnl_R, "bars": i + 1, "exit_price": exit_price}

            if tp1_breach:
                # Partial close 50% at TP1, move SL to BE
                tp1_exit = tp1 * (1 - SLIPPAGE) if is_long else tp1 * (1 + SLIPPAGE)
                tp1_pnl = (qty * 0.5) * (tp1_exit - fill_price) if is_long else (qty * 0.5) * (fill_price - tp1_exit)
                tp1_fee = (exposure * 0.5) * TAKER_FEE
                tp1_hit = True
                # SL moves to entry (BE)
                continue

            if sl_breach:
                exit_price = sl * (1 - SLIPPAGE) if is_long else sl * (1 + SLIPPAGE)
                pnl_gross = qty * (exit_price - fill_price) if is_long else qty * (fill_price - exit_price)
                exit_fee = exposure * TAKER_FEE
                pnl_net = pnl_gross - entry_fee - exit_fee
                pnl_R = pnl_net / risk_usd
                return {"outcome": "SL", "pnl_R": pnl_R, "bars": i + 1, "exit_price": exit_price}
        else:
            # TP1 hit, watching TP2 vs BE-SL on remaining 50%
            be = sl_after_tp1
            sl_breach = (is_long and l <= be) or (not is_long and h >= be)
            tp2_breach = (is_long and h >= tp2) or (not is_long and l <= tp2)

            if sl_breach and tp2_breach:
                # Conservative: BE-SL wins
                exit_price = be
                remaining_pnl = (qty * 0.5) * (exit_price - fill_price) if is_long else (qty * 0.5) * (fill_price - exit_price)
                exit_fee = (exposure * 0.5) * TAKER_FEE
                pnl_net = tp1_pnl + remaining_pnl - entry_fee - tp1_fee - exit_fee
                pnl_R = pnl_net / risk_usd
                return {"outcome": "SL_BE", "pnl_R": pnl_R, "bars": i + 1, "exit_price": exit_price}

            if tp2_breach:
                exit_price = tp2 * (1 - SLIPPAGE) if is_long else tp2 * (1 + SLIPPAGE)
                remaining_pnl = (qty * 0.5) * (exit_price - fill_price) if is_long else (qty * 0.5) * (fill_price - exit_price)
                exit_fee = (exposure * 0.5) * TAKER_FEE
                pnl_net = tp1_pnl + remaining_pnl - entry_fee - tp1_fee - exit_fee
                pnl_R = pnl_net / risk_usd
                return {"outcome": "TP2", "pnl_R": pnl_R, "bars": i + 1, "exit_price": exit_price}

            if sl_breach:
                exit_price = be
                remaining_pnl = (qty * 0.5) * (exit_price - fill_price) if is_long else (qty * 0.5) * (fill_price - exit_price)
                exit_fee = (exposure * 0.5) * TAKER_FEE
                pnl_net = tp1_pnl + remaining_pnl - entry_fee - tp1_fee - exit_fee
                pnl_R = pnl_net / risk_usd
                return {"outcome": "SL_BE", "pnl_R": pnl_R, "bars": i + 1, "exit_price": exit_price}

    # Expired without hitting TP2 or SL
    last_close = float(sim_df["close"].iloc[-1])
    if tp1_hit:
        # Close remaining at last close
        exit_price = last_close
        remaining_pnl = (qty * 0.5) * (exit_price - fill_price) if is_long else (qty * 0.5) * (fill_price - exit_price)
        exit_fee = (exposure * 0.5) * TAKER_FEE
        pnl_net = tp1_pnl + remaining_pnl - entry_fee - tp1_fee - exit_fee
    else:
        exit_price = last_close
        pnl_gross = qty * (exit_price - fill_price) if is_long else qty * (fill_price - exit_price)
        exit_fee = exposure * TAKER_FEE
        pnl_net = pnl_gross - entry_fee - exit_fee

    pnl_R = pnl_net / risk_usd
    outcome = "EXPIRED_TP1" if tp1_hit else "EXPIRED"
    return {"outcome": outcome, "pnl_R": pnl_R, "bars": len(sim_df), "exit_price": exit_price}


def run_backtest(strategy_name: str, pairs: list[str], step_bars: int = 1) -> list[dict]:
    """
    Walk-forward backtest a single strategy across pairs.

    step_bars: how many ob_tf bars to advance between scans. 1 = scan every bar (slow but thorough).
    """
    strategy = next((s for s in STRATEGIES if s["name"] == strategy_name), None)
    if not strategy:
        raise ValueError(f"Unknown strategy: {strategy_name}")

    htf_tfs = strategy["htf_tfs"]
    ob_tf = strategy["ob_tf"]
    entry_tf = strategy["entry_tf"]
    needed_tfs = list(dict.fromkeys(htf_tfs + [ob_tf, entry_tf]))
    sim_tf = ENTRY_TF_FOR_SIM.get(strategy_name, "15m")
    if sim_tf not in needed_tfs:
        needed_tfs.append(sim_tf)

    print(f"\n=== Backtesting {strategy_name.upper()} ===")
    print(f"TFs needed: {needed_tfs}")
    print(f"Pairs: {pairs}")

    # Cooldown: per (symbol, direction), respect cooldown hours
    cooldown_hours = {"swing": 8, "intraday": 4, "scalp": 2}.get(strategy_name, 4)
    last_signal = {}  # (symbol, direction) → ts

    all_trades = []

    for symbol in pairs:
        # Load all needed TFs for this pair
        tf_data = {}
        missing = False
        for tf in needed_tfs:
            df = load_csv(symbol, tf)
            if df is None or len(df) == 0:
                print(f"  SKIP {symbol}: missing {tf}")
                missing = True
                break
            tf_data[tf] = df
        if missing:
            continue

        ob_df = tf_data[ob_tf]
        sim_df_full = tf_data[sim_tf]

        # Precompute int64 timestamp arrays for fast searchsorted slicing
        ts_arrays = {tf: tf_data[tf]["timestamp"].astype("int64").to_numpy() for tf in needed_tfs}

        # Determine warm-up: need enough history for fractal swings + flag detection
        warmup = max(50, 2 * 30)  # 50 candles minimum
        # Determine end: leave at least max_sim_bars worth of sim data for last trades
        # Trade simulation starts AFTER ob_tf candle close, so need future sim_df
        end_idx = len(ob_df)

        signals = 0
        trades_this_pair = 0
        scan_count = 0
        last_progress_pct = -1

        for i in range(warmup, end_idx, step_bars):
            scan_count += 1
            # progress dots every 10%
            pct = int((i - warmup) / max(1, end_idx - warmup) * 10)
            if pct > last_progress_pct:
                print(f".", end="", flush=True)
                last_progress_pct = pct
            cur_ts = ob_df["timestamp"].iloc[i]

            # Build mtf_data slices (≤ cur_ts) — searchsorted O(log n)
            mtf_slice = {}
            for tf in needed_tfs:
                mtf_slice[tf] = slice_up_to(tf_data[tf], cur_ts, ts_arrays[tf])

            # Skip if any TF too short
            if any(len(d) < 30 for d in mtf_slice.values()):
                continue

            try:
                setups = entry_engine.analyze_pair(symbol, mtf_slice, strategy)
            except Exception as e:
                continue

            for setup in setups:
                signals += 1
                direction = setup["direction"]
                # Cooldown check
                ck = (symbol, direction)
                last_ts = last_signal.get(ck)
                if last_ts and (cur_ts - last_ts).total_seconds() / 3600 < cooldown_hours:
                    continue
                last_signal[ck] = cur_ts

                # For backtest fairness: only execute non-approaching signals
                # (approaching = pending limit order, harder to simulate accurately)
                if setup.get("approaching", False):
                    continue

                # Simulate trade — get future sim_tf candles after cur_ts
                future = sim_df_full[sim_df_full["timestamp"] > cur_ts].reset_index(drop=True)
                if len(future) < 5:
                    continue

                entry = setup["current_price"]
                sl = setup["sl"]
                tp1 = setup["tp1"]
                tp2 = setup["tp2"]

                # Risk per trade: 1.5% of $100 base
                risk_usd = 100 * 0.015

                outcome = simulate_trade(
                    entry, sl, tp1, tp2, direction, future, risk_usd, strategy_name
                )

                trade = {
                    "symbol": symbol,
                    "strategy": strategy_name,
                    "direction": direction,
                    "entry_ts": cur_ts.isoformat(),
                    "entry_price": round(entry, 6),
                    "sl": round(sl, 6),
                    "tp1": round(tp1, 6),
                    "tp2": round(tp2, 6),
                    "rr_tp2": setup.get("rr_tp2", 0),
                    "score": setup.get("confidence", 0),
                    "flag_pattern": setup.get("flag_pattern") and setup["flag_pattern"]["type"],
                    "outcome": outcome["outcome"],
                    "pnl_R": round(outcome["pnl_R"], 4),
                    "bars": outcome["bars"],
                }
                all_trades.append(trade)
                trades_this_pair += 1

        print(f" {symbol}: scanned={scan_count}, signals={signals}, trades={trades_this_pair}", flush=True)

    return all_trades


def summarize(trades: list[dict], group_by: str | None = None) -> dict:
    if not trades:
        return {"total": 0}

    if group_by:
        groups = defaultdict(list)
        for t in trades:
            groups[t.get(group_by, "unknown")].append(t)
        return {k: summarize(v) for k, v in groups.items()}

    n = len(trades)
    pnl_R = [t["pnl_R"] for t in trades]
    wins = [r for r in pnl_R if r > 0]
    losses = [r for r in pnl_R if r <= 0]
    total_R = sum(pnl_R)
    wr = len(wins) / n * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    expectancy = total_R / n

    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")

    # Outcome breakdown
    outcome_counts = defaultdict(int)
    for t in trades:
        outcome_counts[t["outcome"]] += 1

    # Equity curve & max DD (in R)
    equity = [0.0]
    for r in pnl_R:
        equity.append(equity[-1] + r)
    peak = equity[0]
    max_dd = 0.0
    for x in equity:
        if x > peak:
            peak = x
        dd = peak - x
        if dd > max_dd:
            max_dd = dd

    return {
        "total": n,
        "win_rate": round(wr, 1),
        "wins": len(wins),
        "losses": len(losses),
        "total_R": round(total_R, 2),
        "expectancy_R": round(expectancy, 3),
        "avg_win_R": round(avg_win, 2),
        "avg_loss_R": round(avg_loss, 2),
        "profit_factor": round(pf, 2) if pf != float("inf") else "inf",
        "max_dd_R": round(max_dd, 2),
        "outcomes": dict(outcome_counts),
    }


def main():
    strategies_to_test = ["intraday", "swing"]
    if len(sys.argv) > 1:
        strategies_to_test = [sys.argv[1]]

    pairs = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
             "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT"]
    if len(sys.argv) > 2:
        pairs = [f"{p}/USDT" if "/" not in p else p for p in sys.argv[2].split(",")]

    # Step bars: scan less often than cooldown allows = no info loss.
    # intraday: cooldown=4h, ob_tf=1h → step=4 (every 4h, ~1800 scans/pair over 300d)
    # swing: cooldown=8h, ob_tf=4h → step=2 (every 8h, ~1100 scans/pair over 365d)
    step_map = {"intraday": 4, "swing": 2, "scalp": 1}

    all_trades = []
    for strat in strategies_to_test:
        trades = run_backtest(strat, pairs, step_bars=step_map.get(strat, 1))
        all_trades.extend(trades)

    # Save raw trades
    out_path = RESULTS_DIR / f"trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w") as f:
        json.dump(all_trades, f, indent=2, default=str)
    print(f"\nTrades saved: {out_path}")

    # Print summaries
    print(f"\n{'='*60}")
    print("OVERALL")
    print(f"{'='*60}")
    print(json.dumps(summarize(all_trades), indent=2))

    print(f"\n{'='*60}")
    print("PER STRATEGY")
    print(f"{'='*60}")
    print(json.dumps(summarize(all_trades, "strategy"), indent=2))

    print(f"\n{'='*60}")
    print("PER SYMBOL")
    print(f"{'='*60}")
    print(json.dumps(summarize(all_trades, "symbol"), indent=2))


if __name__ == "__main__":
    main()
