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

# Top 15 liquid pairs — for swing (clean structure, high liquidity)
SWING_PAIRS: list[str] = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT", "TRX/USDT",
    "LTC/USDT", "BCH/USDT", "ATOM/USDT", "UNI/USDT", "APT/USDT",
]

# Top 12 liquid pairs — for intraday (dikurangi dari 20 agar scan selesai < 5 menit)
INTRADAY_PAIRS: list[str] = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
    "TRX/USDT", "LTC/USDT",
]

# Top 30 liquid pairs — for scalping (more variety = more zone opportunities)
SCALP_PAIRS: list[str] = [
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
    "TRX/USDT", "LTC/USDT", "BCH/USDT", "ATOM/USDT", "UNI/USDT",
    "ARB/USDT", "NEAR/USDT", "APT/USDT", "OP/USDT", "SUI/USDT",
    "INJ/USDT", "FIL/USDT", "HBAR/USDT", "ETC/USDT", "AAVE/USDT",
    "XLM/USDT", "ICP/USDT", "VET/USDT", "SAND/USDT", "MANA/USDT",
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
MIN_RR_RATIO: float = float(_get_env("MIN_RR_RATIO", 2.0))

# ── Alert thresholds ─────────────────────────────────────────────────────────
MIN_CONFIDENCE_SCORE: int = int(_get_env("MIN_CONFIDENCE_SCORE", 3))

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE: str = _get_env("TIMEZONE", "Asia/Jakarta")

# ── Proxy (opsional) ─────────────────────────────────────────────────────────
HTTP_PROXY: str = _get_env("HTTP_PROXY", "")
HTTPS_PROXY: str = _get_env("HTTPS_PROXY", "")

# ── Analysis parameters ───────────────────────────────────────────────────────
CANDLE_LIMIT: int = 200          # candles per timeframe fetch (15m uses 500)
CANDLE_LIMIT_15M: int = 500      # 15m needs more candles for adequate history
ATR_PERIOD: int = 14
SWING_LOOKBACK: int = 3          # fractal swing detection: n candles left+right
OB_MAX_TRACKED: int = 8          # max order blocks tracked per direction
EQH_EQL_TOLERANCE: float = 0.003  # 0.3% tolerance for equal highs/lows
OB_BUFFER_PCT: float = 0.005     # 0.5% buffer above/below OB for SL placement
OB_MOVE_MULTIPLIER: float = 1.5  # impulsive move threshold: n * ATR (1.5→more bearish OBs detected)
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
        "pairs": SWING_PAIRS,
        "scan_interval_minutes": 15,
        # Backtest evidence (365d, 105 trades, v1): WR 64.8%, +14.94R, PF 1.4.
        # Higher scores (5+) actually LOSE in swing — score=4 is sweet spot. Keep v1.
        # Filter analysis: no additional filter (RR>=3, score>=5, no_flag) improves total return.
        "min_confidence": 4,
        "min_rr": 2.5,
        "max_signals_per_scan": 3,
        "ob_fresh_lookback": 80,           # C2: 4H×80 = ~13 days OB history (fresher OBs)
        "ob_approach_pct": 0.03,
        "ob_sl_buffer": 0.010,            # 1.0% buffer — 4H OBs need more room than 1H
        "min_sl_pct": 0.008,              # SL minimum 0.8% from entry
        "max_htf_conflicts": 1,           # swing is strict: max 1 TF conflict
        "max_24h_change_pct": 0.08,       # skip if coin moved >8% in last 24h (crash/spike guard)
        "paper_sizing": "risk_based",     # 10x leverage → fixed margin too aggressive; size by risk amount
        "paper_risk_pct": 0.015,          # 1.5% balance risk per trade
    },
    {
        "name": "intraday",
        "label": "Intraday",
        "emoji": "⏱️",
        "htf_tfs": ["4h", "1h", "15m"],
        "ob_tf": "1h",
        "entry_tf": "5m",
        "pairs": INTRADAY_PAIRS,
        "scan_interval_minutes": 10,       # 5→10 menit: 12 pair × 3 TF butuh waktu, hindari job skip
        # Backtest evidence (300d, 162 baseline trades, v1):
        #   - Baseline: WR 51.9%, +2.7R, PF 1.06 (basically break-even)
        #   - Filter min_rr=3.0 alone → 45 trades, WR 62.2%, +11.36R, PF 2.44, max DD -3.71R
        #   - Filter min_rr=3.0 dominates ALL other filters tested (score, no_flag, weighted scoring)
        # SINGLE CHANGE from baseline: min_rr 1.5 → 3.0
        "min_confidence": 3,
        "min_rr": 3.0,                    # v2: 1.5→3.0 (key edge — RR 3+ winners, RR<3 losers)
        "ob_fresh_lookback": 168,
        "ob_approach_pct": 0.03,
        "max_signals_per_scan": 3,
        "ob_sl_buffer": 0.007,
        "min_sl_pct": 0.005,
        "max_htf_conflicts": 2,           # restore to baseline (max_htf_conflicts=1 didn't help in data)
        "max_24h_change_pct": 0.10,
        "paper_sizing": "risk_based",
        "paper_risk_pct": 0.015,          # 1.5% balance risk per trade
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
        "min_confidence": 5,
        "min_rr": 1.5,
        "ob_fresh_lookback": 50,
        "max_signals_per_scan": 2,
        "ob_sl_buffer": 0.005,            # 0.5% buffer — 15m OBs
        "min_sl_pct": 0.003,              # SL minimum 0.3% dari entry (10x leverage)
    },
]


# ── Paper Trading ─────────────────────────────────────────────────────────────
PAPER_TRADING_ENABLED: bool = _get_env("PAPER_TRADING_ENABLED", "true").lower() == "true"
PAPER_INITIAL_BALANCE: float = float(_get_env("PAPER_INITIAL_BALANCE", 100.0))
PAPER_LEVERAGE: int = 10
PAPER_MAX_POSITIONS: int = 3               # max 3 posisi concurrent (1 per daily slot)
PAPER_MAX_DAILY_TRADES: int = 4           # hard cap 4 trade per hari
PAPER_MAX_PENDING: int = 2            # max pending limit orders at a time
PAPER_MARGIN_PER_TRADE_PCT: float = 0.45  # 45% balance per posisi → $45 × 10x = $450 exposure
PAPER_STATE_FILE: str = _get_env("PAPER_STATE_FILE", str(_BASE_DIR / "paper_state.json"))
PAPER_FORCE_PAIRS: list[str] = [          # 10 pair utama untuk forced intraday scan
    "BTC/USDT", "ETH/USDT", "BNB/USDT", "SOL/USDT", "XRP/USDT",
    "DOGE/USDT", "ADA/USDT", "AVAX/USDT", "DOT/USDT", "LINK/USDT",
]
PAPER_MIN_DAILY_TRADES: int = 3           # wajib minimal 3 trade intraday per hari


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
