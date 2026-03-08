"""
Quick diagnostic: shows market state for all scalp pairs without signal filters.
Run: python diagnose.py
"""
import sys
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

from config.settings import STRATEGIES, SCALP_PAIRS
from core.data_fetcher import DataFetcher
from core import market_structure, order_blocks, fvg as fvg_mod
from core import data_fetcher as df_module
from core.premium_discount import calculate as pd_calc

fetcher = DataFetcher()

scalp_strat = next(s for s in STRATEGIES if s["name"] == "scalp")

print(f"\n{'='*60}")
print(f"{'PAIR':<12} {'1H':^10} {'15m':^10} {'OB':^5} {'FVG':^5} {'Zone':^12} {'BOS':^5}")
print(f"{'='*60}")

for sym in SCALP_PAIRS:
    perp = f"{sym}:USDT"
    try:
        mtf = fetcher.fetch_for_strategy(perp, scalp_strat)
        if not mtf or any(v is None for v in mtf.values()):
            print(f"{sym:<12} {'NO DATA':^46}")
            continue

        df_1h  = mtf["1h"]
        df_15m = mtf["15m"]

        s1h  = market_structure.analyze(df_1h)
        s15m = market_structure.analyze(df_15m)

        price = float(df_15m["close"].iloc[-1])
        atr   = df_module.compute_atr(df_15m)
        obs   = order_blocks.detect(df_15m, atr, s15m)
        fvgs  = fvg_mod.detect(df_15m)

        # Check if price is inside any OB or FVG
        def in_zone(zones, price):
            return any(z["zone_low"] <= price <= z["zone_high"] for z in zones)

        bull_ob = in_zone(obs["bullish"], price)
        bear_ob = in_zone(obs["bearish"], price)
        bull_fvg = in_zone(fvgs["bullish"], price)
        bear_fvg = in_zone(fvgs["bearish"], price)

        ob_str  = "BULL" if bull_ob else ("BEAR" if bear_ob else "-")
        fvg_str = "BULL" if bull_fvg else ("BEAR" if bear_fvg else "-")

        sh = s15m.get("last_swing_high")
        sl = s15m.get("last_swing_low")
        pd_res = pd_calc(sh, sl, price) if sh and sl else {"zone": "?", "position_pct": 0}
        zone_str = f"{pd_res['zone']}({pd_res['position_pct']:.0f}%)"

        bos = s1h.get("last_bos")
        bos_str = bos["direction"][:4] if bos else "-"

        print(f"{sym:<12} {s1h['trend']:^10} {s15m['trend']:^10} {ob_str:^5} {fvg_str:^5} {zone_str:^12} {bos_str:^5}  ${price:.4f}")

    except Exception as e:
        print(f"{sym:<12} ERROR: {e}")

print(f"{'='*60}\n")
