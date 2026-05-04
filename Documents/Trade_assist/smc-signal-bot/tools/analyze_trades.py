"""
Deep analysis of backtest trades — segments by score, hour, direction, pair, score-component, etc.

Usage:
  python tools/analyze_trades.py backtest_results/trades_<ts>.json
"""
import sys
import json
import statistics
from collections import defaultdict
from pathlib import Path
from datetime import datetime


def winrate(trades):
    if not trades:
        return None
    wins = sum(1 for t in trades if t["pnl_R"] > 0)
    return wins / len(trades) * 100


def metrics(trades):
    if not trades:
        return {}
    n = len(trades)
    pnl_R = [t["pnl_R"] for t in trades]
    wins_R = [r for r in pnl_R if r > 0]
    losses_R = [r for r in pnl_R if r <= 0]
    total_R = sum(pnl_R)
    wr = len(wins_R) / n * 100
    expectancy = total_R / n
    avg_win = sum(wins_R)/len(wins_R) if wins_R else 0
    avg_loss = sum(losses_R)/len(losses_R) if losses_R else 0
    pf = abs(sum(wins_R)/sum(losses_R)) if losses_R and sum(losses_R) != 0 else float("inf")

    eq = [0.0]
    for r in pnl_R:
        eq.append(eq[-1] + r)
    peak = eq[0]
    max_dd = 0
    for x in eq:
        if x > peak: peak = x
        if peak - x > max_dd: max_dd = peak - x

    return {
        "n": n,
        "wr%": round(wr, 1),
        "expectancy": round(expectancy, 3),
        "total_R": round(total_R, 2),
        "PF": round(pf, 2) if pf != float("inf") else "inf",
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_dd_R": round(max_dd, 2),
    }


def hour_of(t):
    """Hour in WIB (UTC+7)."""
    dt = datetime.fromisoformat(t["entry_ts"])
    return (dt.hour + 7) % 24


def main(path):
    trades = json.load(open(path))
    print(f"\nLoaded {len(trades)} trades from {path}\n")

    print("="*70)
    print("OVERALL")
    print("="*70)
    print(metrics(trades))

    # By strategy
    print("\n" + "="*70)
    print("BY STRATEGY")
    print("="*70)
    by_strat = defaultdict(list)
    for t in trades:
        by_strat[t["strategy"]].append(t)
    for k, v in by_strat.items():
        print(f"\n  {k}: {metrics(v)}")

    # By symbol
    print("\n" + "="*70)
    print("BY SYMBOL")
    print("="*70)
    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)
    for k in sorted(by_sym.keys()):
        print(f"  {k:10}: {metrics(by_sym[k])}")

    # By direction
    print("\n" + "="*70)
    print("BY DIRECTION")
    print("="*70)
    by_dir = defaultdict(list)
    for t in trades:
        by_dir[t["direction"]].append(t)
    for k, v in by_dir.items():
        print(f"  {k:6}: {metrics(v)}")

    # By score
    print("\n" + "="*70)
    print("BY SCORE (confluence)")
    print("="*70)
    by_score = defaultdict(list)
    for t in trades:
        by_score[t["score"]].append(t)
    for k in sorted(by_score.keys()):
        print(f"  score={k}: {metrics(by_score[k])}")

    # By outcome
    print("\n" + "="*70)
    print("BY OUTCOME")
    print("="*70)
    by_oc = defaultdict(list)
    for t in trades:
        by_oc[t["outcome"]].append(t)
    for k in sorted(by_oc.keys()):
        v = by_oc[k]
        avgR = sum(t["pnl_R"] for t in v) / len(v)
        print(f"  {k:14}: n={len(v)}, avg_R={avgR:+.3f}")

    # By WIB hour
    print("\n" + "="*70)
    print("BY WIB HOUR (entry)")
    print("="*70)
    by_hour = defaultdict(list)
    for t in trades:
        by_hour[hour_of(t)].append(t)
    print("  hour | n   | wr%   | expectancy")
    for h in range(24):
        v = by_hour.get(h, [])
        if v:
            m = metrics(v)
            print(f"  {h:>4} | {m['n']:>3} | {m['wr%']:>5} | {m['expectancy']:+.3f}")

    # By flag pattern
    print("\n" + "="*70)
    print("BY FLAG PATTERN")
    print("="*70)
    flag = [t for t in trades if t["flag_pattern"]]
    no_flag = [t for t in trades if not t["flag_pattern"]]
    print(f"  with_flag:   {metrics(flag)}")
    print(f"  no_flag:     {metrics(no_flag)}")

    # By RR bucket
    print("\n" + "="*70)
    print("BY RR_TP2 BUCKET")
    print("="*70)
    buckets = {"<2": [], "2-3": [], "3-4": [], ">=4": []}
    for t in trades:
        r = t["rr_tp2"]
        if r < 2: buckets["<2"].append(t)
        elif r < 3: buckets["2-3"].append(t)
        elif r < 4: buckets["3-4"].append(t)
        else: buckets[">=4"].append(t)
    for k, v in buckets.items():
        print(f"  RR {k:>4}: {metrics(v) if v else 'no trades'}")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if not path:
        # find latest
        results = sorted(Path("backtest_results").glob("trades_*.json"))
        if not results:
            print("No trades file found. Pass path as argument.")
            sys.exit(1)
        path = results[-1]
        print(f"Using latest: {path}")
    main(path)
