"""
Settings module - loads configuration from .env file and exposes typed constants.
All other modules import from here, never directly from os.environ.
"""

import os
from pathlib import Path
from dotenv import load_dotenv
import ccxt

# Load .env from project root
_BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BASE_DIR / ".env")


def _get_env(key: str, default=None, required: bool = False):
    value = os.getenv(key, default)
    if required and not value:
        raise ValueError(f"Required environment variable '{key}' is not set. Check your .env file.")
    return value


# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = _get_env("TELEGRAM_BOT_TOKEN", required=False)
TELEGRAM_CHAT_ID: str = _get_env("TELEGRAM_CHAT_ID", required=False)

# ── Trading pairs ─────────────────────────────────────────────────────────────
_raw_pairs = _get_env("TRADING_PAIRS", "BTC/USDT,ETH/USDT,SOL/USDT")
TRADING_PAIRS: list[str] = [p.strip() for p in _raw_pairs.split(",")]

# Perpetual symbol map: BTC/USDT -> BTC/USDT:USDT
PERP_PAIRS: list[str] = [f"{p}:USDT" for p in TRADING_PAIRS]

# Top 10 by market cap — for scalping
SCALP_PAIRS: list[str] = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
]

TIMEFRAMES: dict[str, str] = {
    "15m": "15m",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1d",
}

# Candle limits per timeframe
CANDLE_LIMITS: dict[str, int] = {
    "1d":  200,
    "4h":  200,
    "1h":  300,
    "15m": 500,
    "5m":  300,
    "3m":  200,
    "1m":  200,
}

# ── Scan schedule ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES: int = int(_get_env("SCAN_INTERVAL_MINUTES", 15))
MARKET_UPDATE_HOURS: int = int(_get_env("MARKET_UPDATE_HOURS", 4))

# ── Risk management ──────────────────────────────────────────────────────────
DEFAULT_CAPITAL: float = float(_get_env("DEFAULT_CAPITAL", 1_000_000))
RISK_PER_TRADE_PERCENT: float = float(_get_env("RISK_PER_TRADE_PERCENT", 1))
MAX_LEVERAGE: int = int(_get_env("MAX_LEVERAGE", 3))
MIN_RR_RATIO: float = float(_get_env("MIN_RR_RATIO", 3.0))

# ── Alert thresholds ─────────────────────────────────────────────────────────
MIN_CONFIDENCE_SCORE: int = int(_get_env("MIN_CONFIDENCE_SCORE", 4))

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE: str = _get_env("TIMEZONE", "Asia/Jakarta")

# ── Proxy (opsional) ─────────────────────────────────────────────────────────
HTTP_PROXY: str = _get_env("HTTP_PROXY", "")
HTTPS_PROXY: str = _get_env("HTTPS_PROXY", "")

# ── Analysis parameters ───────────────────────────────────────────────────────
CANDLE_LIMIT: int = 200          # candles per timeframe fetch (15m uses 500)
CANDLE_LIMIT_15M: int = 500      # 15m needs more candles for adequate history
ATR_PERIOD: int = 14
SWING_LOOKBACK: int = 2          # fractal swing detection: n candles left+right
OB_MAX_TRACKED: int = 5          # max order blocks tracked per direction
EQH_EQL_TOLERANCE: float = 0.002  # 0.2% tolerance for equal highs/lows
OB_BUFFER_PCT: float = 0.005     # 0.5% buffer above/below OB for SL placement
OB_MOVE_MULTIPLIER: float = 2.0  # impulsive move threshold: n * ATR
OB_PRICE_FLOOR_PCT: float = 0.005  # minimum 0.5% of price for ATR floor
LIQUIDITY_PROXIMITY_PCT: float = 0.005  # 0.5% proximity for liquidity check

# ── Strategy definitions ───────────────────────────────────────────────────────
# Each strategy has its own TF hierarchy, OB detection TF, entry confirmation TF,
# pair list, and quality thresholds.
STRATEGIES: list[dict] = [
    {
        "name": "swing",
        "label": "Swing Trading",
        "emoji": "📈",
        "htf_tfs": ["1d", "4h", "1h"],   # all must align
        "ob_tf": "4h",                     # OB/FVG detected on this TF
        "entry_tf": "15m",                 # user watches this TF for confirmation
        "pairs": TRADING_PAIRS,
        "scan_interval_minutes": 15,
        "min_confidence": 4,
        "min_rr": 3.0,
        "ob_fresh_lookback": 50,
        "ob_sl_buffer": 0.010,            # 1.0% buffer — 4H OBs need more room
    },
    {
        "name": "intraday",
        "label": "Intraday",
        "emoji": "⏱️",
        "htf_tfs": ["4h", "1h", "15m"],
        "ob_tf": "1h",
        "entry_tf": "5m",
        "pairs": TRADING_PAIRS,
        "scan_interval_minutes": 5,
        "min_confidence": 4,
        "min_rr": 2.5,
        "ob_fresh_lookback": 40,
        "ob_sl_buffer": 0.007,            # 0.7% buffer — 1H OBs
    },
    {
        "name": "scalp",
        "label": "Scalping",
        "emoji": "⚡",
        "htf_tfs": ["1h", "15m"],
        "ob_tf": "15m",
        "entry_tf": "5m",
        "pairs": SCALP_PAIRS,
        "scan_interval_minutes": 5,
        "min_confidence": 3,
        "min_rr": 2.0,
        "ob_fresh_lookback": 30,
        "ob_sl_buffer": 0.005,            # 0.5% buffer — 15m OBs
    },
]


def get_exchange() -> ccxt.gate:
    """Create and return a configured ccxt Gate.io exchange instance for USDT perpetual futures.

    Gate.io is used as the data source because it is accessible from Indonesia
    without a VPN (unlike Bybit/Binance which are blocked by local ISPs).
    """
    config = {
        "enableRateLimit": True,
        "timeout": 30000,
        "options": {
            "defaultType": "swap",  # USDT-margined perpetual futures
        },
    }
    # Add proxy if configured in .env
    if HTTP_PROXY or HTTPS_PROXY:
        config["proxies"] = {
            "http": HTTP_PROXY or HTTPS_PROXY,
            "https": HTTPS_PROXY or HTTP_PROXY,
        }
    exchange = ccxt.gate(config)
    return exchange
