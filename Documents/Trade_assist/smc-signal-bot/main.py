"""
SMC Signal Bot - Main entry point.
Initializes all modules and runs the APScheduler for periodic scanning.
"""

import sys
import signal
import logging

# ── Windows asyncio fix (must be before any asyncio/telegram imports) ─────────
if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
load_dotenv()

import pytz
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR

from config.settings import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TRADING_PAIRS,
    MARKET_UPDATE_HOURS,
    TIMEZONE,
    STRATEGIES,
)
from core.data_fetcher import DataFetcher
from core import market_structure as ms
from core import entry_engine
from alerts.telegram_alert import TelegramAlerter
from alerts.command_handler import TelegramCommandHandler
from utils.logger import get_logger

logger = get_logger("smc_bot")

# ── Globals ────────────────────────────────────────────────────────────────────
_fetcher: DataFetcher | None = None
_alerter: TelegramAlerter | None = None
_scheduler: BlockingScheduler | None = None
_cmd_handler: TelegramCommandHandler | None = None


# ─── Scan jobs ─────────────────────────────────────────────────────────────────

def _scan_strategy(strategy: dict) -> None:
    """Scan all pairs for a given strategy."""
    pairs = strategy["pairs"]
    perp_pairs = [f"{p}:USDT" for p in pairs]
    logger.info(f"=== [{strategy['name'].upper()}] Scan started ({len(pairs)} pairs) ===")

    for display_sym, perp_sym in zip(pairs, perp_pairs):
        try:
            mtf_data = _fetcher.fetch_for_strategy(perp_sym, strategy)

            failed = [tf for tf, df in mtf_data.items() if df is None]
            if failed:
                logger.warning(f"{display_sym}: Missing data {failed} — skipping")
                continue

            setups = entry_engine.analyze_pair(display_sym, mtf_data, strategy)
            for setup in setups:
                _alerter.send_signal_alert(setup)

        except Exception as e:
            logger.error(f"Error scanning {display_sym} ({strategy['name']}): {e}", exc_info=True)

    logger.info(f"=== [{strategy['name'].upper()}] Scan complete ===")


def scan_swing() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "swing"))


def scan_intraday() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "intraday"))


def scan_scalp() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "scalp"))


def send_market_update() -> None:
    """Collect market data for all pairs and send the periodic update."""
    logger.info("Preparing market update...")
    market_data = []

    perp_pairs = [f"{p}:USDT" for p in TRADING_PAIRS]
    for display_sym, perp_sym in zip(TRADING_PAIRS, perp_pairs):
        try:
            # Fetch 1D and 4H structure (smaller limit to save API calls)
            import time
            df_1d = _fetcher.fetch_ohlcv(perp_sym, "1d", limit=50)
            time.sleep(0.3)
            df_4h = _fetcher.fetch_ohlcv(perp_sym, "4h", limit=50)
            time.sleep(0.3)
            df_15m = _fetcher.fetch_ohlcv(perp_sym, "15m", limit=3)
            time.sleep(0.3)

            if df_1d is None or df_4h is None or df_15m is None:
                continue

            struct_1d = ms.analyze(df_1d)
            struct_4h = ms.analyze(df_4h)

            current_price = float(df_15m["close"].iloc[-1])

            # 24h change
            df_1d_2 = _fetcher.fetch_ohlcv(perp_sym, "1d", limit=2)
            change_pct = 0.0
            if df_1d_2 is not None and len(df_1d_2) >= 2:
                prev = float(df_1d_2["close"].iloc[-2])
                curr = float(df_1d_2["close"].iloc[-1])
                change_pct = ((curr - prev) / prev * 100) if prev > 0 else 0.0

            market_data.append({
                "symbol": display_sym,
                "current_price": current_price,
                "price_change_pct": round(change_pct, 2),
                "trend_1d": struct_1d["trend"],
                "trend_4h": struct_4h["trend"],
                "key_support": struct_4h.get("last_swing_low"),
                "key_resistance": struct_4h.get("last_swing_high"),
                "has_setup": False,  # simplified for market update
            })

        except Exception as e:
            logger.error(f"Error collecting market data for {display_sym}: {e}", exc_info=True)
            continue

    if market_data:
        _alerter.send_market_update(market_data)
        logger.info("Market update sent")
    else:
        logger.warning("No market data collected — market update skipped")


# ─── APScheduler error handler ─────────────────────────────────────────────────

def _on_job_error(event) -> None:
    if event.exception:
        logger.error(f"Job '{event.job_id}' raised: {event.exception}")


# ─── Graceful shutdown ─────────────────────────────────────────────────────────

def _shutdown(signum, frame) -> None:
    logger.info(f"Received signal {signum} — shutting down gracefully...")
    if _cmd_handler:
        _cmd_handler.stop()
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    sys.exit(0)


# ─── Startup validation ────────────────────────────────────────────────────────

def _validate_config() -> None:
    """Exit early with a clear message if required config is missing."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        print(
            f"\n[ERROR] Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in your Telegram credentials.\n"
        )
        sys.exit(1)


# ─── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    global _fetcher, _alerter, _scheduler, _cmd_handler

    logger.info("=" * 50)
    logger.info("SMC Signal Bot starting up...")
    logger.info("=" * 50)

    _validate_config()

    # Initialize modules
    _fetcher = DataFetcher()
    _alerter = TelegramAlerter()

    # Send startup notification
    all_pairs = sorted(set(p for s in STRATEGIES for p in s["pairs"]))
    _alerter.send_startup_message(all_pairs)

    # Start interactive command handler (handles /price command)
    _cmd_handler = TelegramCommandHandler(TELEGRAM_BOT_TOKEN, _fetcher)
    _cmd_handler.start()

    # Register shutdown handlers
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Configure scheduler
    tz = pytz.timezone(TIMEZONE)
    _scheduler = BlockingScheduler(timezone=tz)
    _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # One scan job per strategy, each with its own interval
    strategy_fns = {
        "swing": scan_swing,
        "intraday": scan_intraday,
        "scalp": scan_scalp,
    }
    for strat in STRATEGIES:
        fn = strategy_fns[strat["name"]]
        _scheduler.add_job(
            fn,
            trigger="interval",
            minutes=strat["scan_interval_minutes"],
            id=f"scan_{strat['name']}",
            name=f"{strat['label']} Scanner",
            misfire_grace_time=60,
            max_instances=1,
        )
        logger.info(
            f"  {strat['emoji']} {strat['label']}: every {strat['scan_interval_minutes']}m "
            f"— {len(strat['pairs'])} pairs, min RR 1:{strat['min_rr']}, "
            f"min confidence {strat['min_confidence']}/5"
        )

    # Market update job
    _scheduler.add_job(
        send_market_update,
        trigger="interval",
        hours=MARKET_UPDATE_HOURS,
        id="market_update",
        name="Market Update",
        misfire_grace_time=300,
        max_instances=1,
    )

    logger.info(f"Watching: {', '.join(TRADING_PAIRS)}")
    logger.info("Bot is running. Press Ctrl+C to stop.")

    # Run the first scan immediately before scheduler kicks in
    try:
        scan_swing()
        scan_intraday()
        scan_scalp()
        send_market_update()
    except Exception as e:
        logger.error(f"Initial scan failed: {e}", exc_info=True)

    # Start blocking scheduler
    try:
        _scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
