"""
Quick summary of a backtest run JSON: overall + per-pair + per-strategy.
Usage: python tools/summarize_run.py <trades.json> [--strategy swing]
"""
import sys
import json
from collections import Counter, defaultdict


def stats(trades):
    if not trades:
        return None
    pnl_R = [t["pnl_R"] for t in trades]
    wins = [r for r in pnl_R if r > 0]
    losses = [r for r in pnl_R if r <= 0]
    n = len(trades)
    total_R = sum(pnl_R)
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else float("inf")
    return {
        "n": n,
        "wr": round(len(wins) / n * 100, 1),
        "total_R": round(total_R, 2),
        "expectancy": round(total_R / n, 3),
        "PF": round(pf, 2) if pf != float("inf") else "inf",
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "outcomes": dict(Counter(t["outcome"] for t in trades)),
    }


def main():
    args = sys.argv[1:]
    if not args:
        print("Usage: python tools/summarize_run.py <trades.json> [--strategy swing]")
        sys.exit(1)
    path = args[0]
    strategy = None
    if "--strategy" in args:
        strategy = args[args.index("--strategy") + 1]

    with open(path) as f:
        trades = json.load(f)
    if strategy:
        trades = [t for t in trades if t["strategy"] == strategy]

    print(f"Source: {path}")
    if strategy:
        print(f"Strategy filter: {strategy}")
    print()
    overall = stats(trades)
    if not overall:
        print("No trades.")
        return
    print(f"OVERALL: n={overall['n']} WR={overall['wr']}% R={overall['total_R']:+.2f} "
          f"exp={overall['expectancy']:+.3f} PF={overall['PF']} "
          f"avg_w={overall['avg_win']:+.2f} avg_l={overall['avg_loss']:+.2f}")
    print(f"Outcomes: {overall['outcomes']}")
    print()
    by_pair = defaultdict(list)
    for t in trades:
        by_pair[t["symbol"]].append(t)
    print(f"{'Pair':<12} {'n':>4} {'WR%':>6} {'R':>8} {'exp':>8} {'PF':>6}")
    for sym in sorted(by_pair):
        s = stats(by_pair[sym])
        print(f"{sym:<12} {s['n']:>4} {s['wr']:>6} {s['total_R']:>+8.2f} "
              f"{s['expectancy']:>+8.3f} {s['PF']:>6}")


if __name__ == "__main__":
    main()
