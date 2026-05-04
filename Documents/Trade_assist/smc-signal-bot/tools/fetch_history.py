"""
Fetch historical OHLCV from Gate.io with pagination, cache to CSV.
Run once to populate backtest_data/ — subsequent backtests reuse cache.

Per-TF day limits (Gate.io enforces max ~10,000 candles back):
  1d: 365d, 4h: 365d, 1h: 300d, 15m: 100d, 5m: 30d
"""
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.settings import get_exchange


CACHE_DIR = Path(__file__).resolve().parent.parent / "backtest_data"
CACHE_DIR.mkdir(exist_ok=True)

PAGE_LIMIT = 1000

PAIRS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT",
]

# Per-TF days back (Gate.io 10000-candle hard limit)
TF_DAYS = {
    "1d":  365,
    "4h":  365,
    "1h":  300,
    "15m": 100,
    "5m":  30,
}


def cache_path(symbol: str, tf: str) -> Path:
    safe = symbol.replace("/", "_").replace(":", "_")
    return CACHE_DIR / f"{safe}_{tf}.csv"


def tf_to_ms(tf: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    return int(tf[:-1]) * units[tf[-1]]


def fetch_history(exchange, symbol: str, tf: str, days: int) -> pd.DataFrame:
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    tf_ms = tf_to_ms(tf)

    perp = f"{symbol}:USDT"
    all_candles = []
    cursor = start_ms
    last_progress = -1
    failures = 0

    while cursor < end_ms:
        try:
            chunk = exchange.fetch_ohlcv(perp, tf, since=cursor, limit=PAGE_LIMIT)
            failures = 0
        except Exception as e:
            failures += 1
            if failures >= 3:
                print(f"\n  ABORT {perp} {tf}: {e}")
                break
            time.sleep(2)
            continue

        if not chunk:
            break

        if all_candles and chunk[0][0] <= all_candles[-1][0]:
            chunk = [c for c in chunk if c[0] > all_candles[-1][0]]
            if not chunk:
                cursor += tf_ms * PAGE_LIMIT
                continue

        all_candles.extend(chunk)
        new_cursor = chunk[-1][0] + tf_ms
        # Sanity: ensure cursor advances. If exchange ignored `since` and returned old data,
        # this would otherwise infinite-loop.
        if new_cursor <= cursor:
            break
        cursor = new_cursor

        # Progress: print one dot per 10% completed
        progress = int((cursor - start_ms) / (end_ms - start_ms) * 10)
        if progress > last_progress:
            print(".", end="", flush=True)
            last_progress = progress

        # Don't break on len(chunk) < PAGE_LIMIT — Gate may cap returns at 999.
        # Loop terminates via cursor >= end_ms or empty chunk.

        time.sleep(0.2)

    if not all_candles:
        return pd.DataFrame()

    df = pd.DataFrame(all_candles, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    df = df.drop_duplicates(subset="timestamp").sort_values("timestamp").reset_index(drop=True)
    return df


def main():
    print(f"Fetching history for {len(PAIRS)} pairs × {len(TF_DAYS)} TFs...")
    exchange = get_exchange()
    exchange.load_markets()
    print(f"Exchange: {len(exchange.markets)} markets\n")

    for symbol in PAIRS:
        for tf, days in TF_DAYS.items():
            path = cache_path(symbol, tf)
            if path.exists():
                existing = pd.read_csv(path, parse_dates=["timestamp"])
                if len(existing) > 0:
                    last_ts = pd.to_datetime(existing["timestamp"].max(), utc=True)
                    age_h = (datetime.now(tz=timezone.utc) - last_ts).total_seconds() / 3600
                    if age_h < 24:
                        print(f"  CACHED  {symbol:10} {tf:4}: {len(existing)} candles")
                        continue

            print(f"  FETCH   {symbol:10} {tf:4} ({days}d) ", end=" ", flush=True)
            t0 = time.time()
            df = fetch_history(exchange, symbol, tf, days)
            elapsed = time.time() - t0
            if len(df) > 0:
                df.to_csv(path, index=False)
                print(f" OK {len(df):>5} candles ({elapsed:.1f}s)")
            else:
                print(f" EMPTY ({elapsed:.1f}s)")

    print("\nDone.")


if __name__ == "__main__":
    main()
