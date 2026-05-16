"""
Data fetcher module - fetches OHLCV market data from Gate.io via ccxt public API.
No API key required for market data.
"""

import time
import threading
import pandas as pd
import numpy as np
import ccxt

from config.settings import (
    get_exchange,
    CANDLE_LIMIT,
    CANDLE_LIMIT_15M,
    CANDLE_LIMITS,
    ATR_PERIOD,
    TIMEFRAMES,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_RETRY_DELAYS = [1, 2, 4]  # seconds, exponential backoff


class DataFetcher:
    """Fetches OHLCV candle data from Gate.io USDT perpetual futures via ccxt public endpoints."""

    def __init__(self):
        # ccxt exchange objects are NOT thread-safe — sharing one across the
        # scan workers + paper-position-check thread + bias check corrupts the
        # internal HTTP session / rate-limiter state and causes permanent hangs.
        # Each thread gets its own exchange instance via thread-local storage.
        self._tls = threading.local()
        # Warm up the main-thread instance so startup logs a sane message.
        try:
            ex = self._get_exchange()
            logger.info(f"DataFetcher initialized — {len(ex.markets)} Gate.io swap markets loaded")
        except Exception as e:
            logger.warning(f"Market pre-load failed (will retry on first fetch): {e}")

    def _get_exchange(self) -> ccxt.gate:
        """Return a ccxt exchange instance unique to the calling thread.

        Lazily creates + loads markets on first access per thread. This avoids
        all cross-thread state corruption in ccxt.
        """
        ex = getattr(self._tls, "exchange", None)
        if ex is None:
            ex = get_exchange()
            try:
                ex.load_markets()
            except Exception as e:
                logger.warning(f"Market load failed for thread exchange: {e}")
            self._tls.exchange = ex
        return ex

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        limit: int | None = None,
    ) -> pd.DataFrame | None:
        """
        Fetch OHLCV candles for a symbol and timeframe.

        Args:
            symbol: Perpetual pair, e.g. 'BTC/USDT:USDT'
            timeframe: Candle interval, e.g. '15m', '1h', '4h', '1d'
            limit: Number of candles to fetch. Defaults to CANDLE_LIMIT or CANDLE_LIMIT_15M.

        Returns:
            DataFrame with columns [timestamp, open, high, low, close, volume]
            or None on failure after retries.
        """
        if limit is None:
            limit = CANDLE_LIMIT_15M if timeframe == "15m" else CANDLE_LIMIT

        for attempt, delay in enumerate([0] + _RETRY_DELAYS, start=1):
            if delay:
                time.sleep(delay)
            try:
                raw = self._get_exchange().fetch_ohlcv(symbol, timeframe, limit=limit)
                if not raw:
                    logger.warning(f"Empty response for {symbol} {timeframe}")
                    return None
                df = pd.DataFrame(
                    raw,
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
                df = df.astype(
                    {"open": float, "high": float, "low": float, "close": float, "volume": float}
                )
                df.reset_index(drop=True, inplace=True)
                logger.debug(f"Fetched {len(df)} candles for {symbol} {timeframe}")
                return df

            except ccxt.NetworkError as e:
                logger.warning(f"Network error fetching {symbol} {timeframe} (attempt {attempt}): {e}")
            except ccxt.ExchangeError as e:
                logger.warning(f"Exchange error fetching {symbol} {timeframe} (attempt {attempt}): {e}")
            except Exception as e:
                logger.error(f"Unexpected error fetching {symbol} {timeframe}: {e}")
                return None  # non-retryable

        logger.error(f"Failed to fetch {symbol} {timeframe} after {len(_RETRY_DELAYS)+1} attempts")
        return None

    def fetch_multi_timeframe(self, symbol: str) -> dict[str, pd.DataFrame | None]:
        """
        Fetch OHLCV data for all configured timeframes for a symbol.

        Args:
            symbol: Perpetual pair, e.g. 'BTC/USDT:USDT'

        Returns:
            Dict mapping timeframe keys to DataFrames, e.g. {'15m': df, '1h': df, ...}
        """
        result: dict[str, pd.DataFrame | None] = {}
        for tf_key, tf_val in TIMEFRAMES.items():
            result[tf_key] = self.fetch_ohlcv(symbol, tf_val)
            time.sleep(0.3)  # avoid hitting rate limits
        logger.info(f"Multi-TF fetch complete for {symbol}")
        return result

    def fetch_for_strategy(
        self,
        symbol: str,
        strategy: dict,
    ) -> dict[str, pd.DataFrame | None]:
        """
        Fetch all timeframes required by a strategy config.

        Args:
            symbol: Perpetual pair, e.g. 'BTC/USDT:USDT'
            strategy: Strategy dict from settings.STRATEGIES

        Returns:
            Dict mapping timeframe → DataFrame for all unique TFs needed.
        """
        # Collect unique TFs: all htf_tfs + ob_tf (deduplicated, preserve order)
        needed = list(dict.fromkeys(strategy["htf_tfs"] + [strategy["ob_tf"]]))
        result: dict[str, pd.DataFrame | None] = {}
        for tf in needed:
            limit = CANDLE_LIMITS.get(tf, CANDLE_LIMIT)
            result[tf] = self.fetch_ohlcv(symbol, tf, limit=limit)
            time.sleep(0.3)
        logger.info(f"Strategy fetch complete for {symbol} ({strategy['name']}): {needed}")
        return result

    def get_current_price(self, symbol: str) -> float | None:
        """
        Get the real-time last traded price for a symbol via ticker.

        Args:
            symbol: Perpetual pair, e.g. 'BTC/USDT:USDT'

        Returns:
            Last traded price as float, or None on failure.
        """
        try:
            ticker = self._get_exchange().fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if price is not None:
                return float(price)
        except Exception as e:
            logger.warning(f"get_current_price ticker failed for {symbol}: {e}")
        # Fallback to OHLCV close
        df = self.fetch_ohlcv(symbol, "1m", limit=2)
        if df is not None and len(df) > 0:
            return float(df["close"].iloc[-1])
        return None

    def get_24h_change_pct(self, symbol: str) -> float | None:
        """
        Calculate 24-hour price change percentage.

        Args:
            symbol: Perpetual pair, e.g. 'BTC/USDT:USDT'

        Returns:
            Percentage change as float (e.g. -1.5 for -1.5%), or None on failure.
        """
        df = self.fetch_ohlcv(symbol, "1d", limit=2)
        if df is not None and len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])
            curr_close = float(df["close"].iloc[-1])
            if prev_close > 0:
                return (curr_close - prev_close) / prev_close * 100
        return None


def compute_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """
    Compute Average True Range (ATR) for a OHLCV DataFrame.

    Args:
        df: DataFrame with columns [high, low, close]
        period: ATR smoothing period (default 14)

    Returns:
        pd.Series of ATR values (NaN for the first `period` rows)
    """
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)

    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.rolling(window=period, min_periods=period).mean()
    return atr
