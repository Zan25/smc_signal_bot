"""
Analytical filter — apply hypothetical filters to existing baseline trades file
to estimate impact without re-running the full backtest.
"""
import sys
import json
from collections import defaultdict


def metrics(trades):
    if not trades:
        return {"n": 0}
    n = len(trades)
    pnl_R = [t["pnl_R"] for t in trades]
    wins = [r for r in pnl_R if r > 0]
    losses = [r for r in pnl_R if r <= 0]
    total_R = sum(pnl_R)
    eq = [0.0]
    for r in pnl_R: eq.append(eq[-1] + r)
    peak, max_dd = eq[0], 0
    for x in eq:
        if x > peak: peak = x
        if peak - x > max_dd: max_dd = peak - x
    return {
        "n": n,
        "wr%": round(len(wins)/n*100, 1),
        "expectancy": round(total_R/n, 3),
        "total_R": round(total_R, 2),
        "PF": round(abs(sum(wins)/sum(losses)), 2) if losses and sum(losses) != 0 else "inf",
        "max_dd": round(max_dd, 2),
    }


def main(path):
    trades = json.load(open(path))
    print(f"Loaded {len(trades)} trades\n")
    print(f"BASELINE all: {metrics(trades)}\n")

    print("="*60)
    print("FILTER: score >= N")
    print("="*60)
    for thresh in [3, 4, 5, 6]:
        filtered = [t for t in trades if t["score"] >= thresh]
        print(f"  score >= {thresh}: {metrics(filtered)}")

    print("\n" + "="*60)
    print("FILTER: skip flag_pattern")
    print("="*60)
    no_flag = [t for t in trades if not t["flag_pattern"]]
    print(f"  no flag: {metrics(no_flag)}")

    print("\n" + "="*60)
    print("FILTER: rr_tp2 >= N")
    print("="*60)
    for rr in [2.0, 2.5, 3.0]:
        filtered = [t for t in trades if t["rr_tp2"] >= rr]
        print(f"  RR >= {rr}: {metrics(filtered)}")

    print("\n" + "="*60)
    print("COMBINED: score>=N + skip_flag + rr>=2.0")
    print("="*60)
    for thresh in [3, 4, 5]:
        filtered = [t for t in trades
                    if t["score"] >= thresh
                    and not t["flag_pattern"]
                    and t["rr_tp2"] >= 2.0]
        print(f"  score>={thresh} + no_flag + RR>=2.0: {metrics(filtered)}")

    print("\n" + "="*60)
    print("COMBINED: score>=5 + skip_flag + rr>=2.5")
    print("="*60)
    filtered = [t for t in trades
                if t["score"] >= 5
                and not t["flag_pattern"]
                and t["rr_tp2"] >= 2.5]
    print(f"  {metrics(filtered)}")


if __name__ == "__main__":
    main(sys.argv[1])
