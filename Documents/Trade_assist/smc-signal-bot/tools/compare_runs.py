"""
Compare two backtest result JSON files side-by-side.
Usage: python tools/compare_runs.py <baseline.json> <variant.json> [--strategy swing]
"""
import sys
import json
from collections import Counter, defaultdict


def metrics(trades):
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
        "PF": round(pf, 2) if pf != float("inf") else float("inf"),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "outcomes": dict(Counter(t["outcome"] for t in trades)),
    }


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("Usage: python tools/compare_runs.py <baseline.json> <variant.json> [--strategy swing]")
        sys.exit(1)
    baseline_path, variant_path = args[0], args[1]
    strategy = None
    if "--strategy" in args:
        strategy = args[args.index("--strategy") + 1]

    with open(baseline_path) as f:
        baseline = json.load(f)
    with open(variant_path) as f:
        variant = json.load(f)
    if strategy:
        baseline = [t for t in baseline if t["strategy"] == strategy]
        variant = [t for t in variant if t["strategy"] == strategy]

    b = metrics(baseline)
    v = metrics(variant)

    print(f"{'Metric':<14} {'Baseline':>12} {'Variant':>12} {'Delta':>14}")
    print("-" * 56)
    for k in ["n", "wr", "total_R", "expectancy", "PF", "avg_win", "avg_loss"]:
        bv = b[k] if b else "-"
        vv = v[k] if v else "-"
        try:
            delta = v[k] - b[k] if isinstance(b[k], (int, float)) and isinstance(v[k], (int, float)) else "-"
        except Exception:
            delta = "-"
        delta_s = f"{delta:+.3f}" if isinstance(delta, float) else str(delta)
        print(f"{k:<14} {str(bv):>12} {str(vv):>12} {delta_s:>14}")
    print()
    print(f"Baseline outcomes: {b['outcomes']}")
    print(f"Variant outcomes:  {v['outcomes']}")

    # Per-pair comparison
    print()
    print(f"{'Pair':<12} {'B_n':>5} {'B_wr':>6} {'B_R':>7}   {'V_n':>5} {'V_wr':>6} {'V_R':>7}   {'dR':>8}")
    print("-" * 72)
    by_pair_b = defaultdict(list)
    by_pair_v = defaultdict(list)
    for t in baseline:
        by_pair_b[t["symbol"]].append(t)
    for t in variant:
        by_pair_v[t["symbol"]].append(t)
    for sym in sorted(set(list(by_pair_b.keys()) + list(by_pair_v.keys()))):
        bm = metrics(by_pair_b.get(sym, [])) or {}
        vm = metrics(by_pair_v.get(sym, [])) or {}
        b_R = bm.get("total_R", 0)
        v_R = vm.get("total_R", 0)
        delta = v_R - b_R
        print(
            f"{sym:<12} {bm.get('n', 0):>5} {bm.get('wr', 0):>6} {b_R:>+7.2f}   "
            f"{vm.get('n', 0):>5} {vm.get('wr', 0):>6} {v_R:>+7.2f}   {delta:>+8.2f}"
        )


if __name__ == "__main__":
    main()
