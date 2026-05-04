"""
SMC Signal Bot - Main entry point.
Initializes all modules and runs the APScheduler for periodic scanning.
"""

import sys
import signal
import logging
import socket
import threading
import json
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError
from http.server import HTTPServer, BaseHTTPRequestHandler

# Global socket timeout — prevents API calls from hanging indefinitely
# Without this, a stuck TCP connection can freeze the entire bot forever
socket.setdefaulttimeout(45)

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
    PAPER_TRADING_ENABLED,
)
from core.data_fetcher import DataFetcher
from core import market_structure as ms
from core import entry_engine
from core.paper_trader import PaperTrader
from alerts.telegram_alert import TelegramAlerter
from alerts.command_handler import TelegramCommandHandler
from utils.logger import get_logger

logger = get_logger("smc_bot")

# ── Globals ────────────────────────────────────────────────────────────────────
_fetcher: DataFetcher | None = None
_alerter: TelegramAlerter | None = None
_scheduler: BlockingScheduler | None = None
_cmd_handler: TelegramCommandHandler | None = None
_paper_trader: PaperTrader | None = None

# ── Signal cooldown tracking ────────────────────────────────────────────────────
# Prevents the same pair+direction from spamming every scan cycle.
# Key: "{symbol}_{direction}" (cross-strategy), Value: (ISO timestamp, strategy_name)
# Persisted to file so cooldown survives bot restarts / Railway redeploys.
_COOLDOWN_FILE = Path(__file__).resolve().parent / "cooldown_state.json"
_signal_cooldown: dict[str, tuple] = {}   # key → (datetime, strategy_name)
_signal_cooldown_lock = threading.Lock()
_COOLDOWN_HOURS: dict[str, int] = {
    "swing": 8,      # swing signals valid 8 hours
    "intraday": 4,   # intraday signals valid 4 hours
    "scalp": 2,      # scalp signals valid 2 hours
}
_MAX_COOLDOWN_HOURS = max(_COOLDOWN_HOURS.values())


def _load_cooldown() -> None:
    """Load cooldown state from file on startup, discarding expired entries."""
    global _signal_cooldown
    if not _COOLDOWN_FILE.exists():
        return
    try:
        with open(_COOLDOWN_FILE) as f:
            raw = json.load(f)
        now = datetime.now()
        loaded = {}
        for key, (ts_str, strat) in raw.items():
            ts = datetime.fromisoformat(ts_str)
            if now - ts < timedelta(hours=_MAX_COOLDOWN_HOURS):
                loaded[key] = (ts, strat)
        with _signal_cooldown_lock:
            _signal_cooldown = loaded
        logger.info(f"[COOLDOWN] Loaded {len(loaded)} active cooldowns from file")
    except Exception as e:
        logger.warning(f"[COOLDOWN] Could not load cooldown state: {e}")


def _save_cooldown() -> None:
    """Persist current cooldown state to file."""
    try:
        with _signal_cooldown_lock:
            data = {k: (ts.isoformat(), strat) for k, (ts, strat) in _signal_cooldown.items()}
        with open(_COOLDOWN_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        logger.warning(f"[COOLDOWN] Could not save cooldown state: {e}")


def _cooldown_key(setup: dict) -> str:
    """
    Build cooldown key. Approaching signals use a separate namespace (_approach suffix)
    so they don't block the real "at zone" signal when price eventually enters the zone.
    """
    base = f"{setup['symbol']}_{setup['direction']}"
    return f"{base}_approach" if setup.get("approaching", False) else base


def _is_on_cooldown(setup: dict) -> bool:
    """Return True if this pair+direction was sent recently (any strategy counts)."""
    key = _cooldown_key(setup)
    with _signal_cooldown_lock:
        entry = _signal_cooldown.get(key)
        if not entry:
            return False
        last_sent, strat = entry
        # Approaching signals: shorter cooldown (1h) so they don't spam but still repeat
        # At-zone signals: full cooldown per strategy
        if setup.get("approaching", False):
            hours = 1.0
        else:
            hours = _COOLDOWN_HOURS.get(setup.get("strategy_name", ""), 4)
        return datetime.now() - last_sent < timedelta(hours=hours)


def _mark_cooldown(setup: dict) -> None:
    """Record that this signal was just sent, then persist to file."""
    key = _cooldown_key(setup)
    with _signal_cooldown_lock:
        _signal_cooldown[key] = (datetime.now(), setup.get("strategy_name", ""))
    _save_cooldown()


# ── Health check HTTP server (keeps Railway happy & allows uptime monitoring) ──
class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass  # suppress access logs


def _start_health_server(port: int = 8080) -> None:
    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True, name="HealthCheck")
        t.start()
        logger.info(f"Health check server started on port {port}")
    except Exception as e:
        logger.warning(f"Could not start health server: {e}")


# ─── Scan jobs ─────────────────────────────────────────────────────────────────

def _scan_pair(display_sym: str, perp_sym: str, strategy: dict) -> tuple:
    """Fetch + analyze a single pair. Run in parallel by ThreadPoolExecutor."""
    try:
        mtf_data = _fetcher.fetch_for_strategy(perp_sym, strategy)
        failed = [tf for tf, df in mtf_data.items() if df is None]
        if failed:
            logger.warning(f"{display_sym}: Missing TF {failed} — skipping")
            return display_sym, []
        return display_sym, entry_engine.analyze_pair(display_sym, mtf_data, strategy)
    except Exception as e:
        logger.error(f"Error scanning {display_sym} ({strategy['name']}): {e}", exc_info=True)
        return display_sym, []


def _get_btc_bias(threshold_pct: float = 0.025) -> str:
    """Check BTC last completed 4H candle to determine market bias.

    Returns 'bearish', 'bullish', or 'neutral'.
    Used to filter counter-trend paper entries on strongly trending days.
    Fails safe to 'neutral' on any fetch error so it never blocks trades unexpectedly.
    """
    try:
        df = _fetcher.fetch_ohlcv("BTC/USDT:USDT", "4h", limit=3)
        if df is None or len(df) < 2:
            return "neutral"
        # Use last COMPLETED candle (iloc[-2]), not the in-progress one
        last = df.iloc[-2]
        change = (float(last["close"]) - float(last["open"])) / float(last["open"])
        if change <= -threshold_pct:
            logger.info(f"[BIAS] BTC 4H last candle: {change*100:.2f}% → BEARISH bias")
            return "bearish"
        if change >= threshold_pct:
            logger.info(f"[BIAS] BTC 4H last candle: {change*100:.2f}% → BULLISH bias")
            return "bullish"
        return "neutral"
    except Exception as e:
        logger.debug(f"[BIAS] BTC bias check failed: {e} — defaulting to neutral")
        return "neutral"


def _scan_strategy(strategy: dict) -> None:
    """Scan all pairs for a given strategy in parallel (4 workers).

    Collects all valid setups first, then sends only the top-N by score
    (max_signals_per_scan) to avoid Telegram spam.
    """
    pairs = strategy["pairs"]
    perp_pairs = [f"{p}:USDT" for p in pairs]
    strat_name = strategy["name"].upper()
    max_signals = strategy.get("max_signals_per_scan", 3)
    logger.info(f"=== [{strat_name}] Scan dimulai ({len(pairs)} pair, parallel) ===")

    # Collect all valid setups from parallel scan
    all_setups: list[dict] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="scan") as executor:
        futures = {
            executor.submit(_scan_pair, sym, perp, strategy): sym
            for sym, perp in zip(pairs, perp_pairs)
        }
        for future in as_completed(futures):
            _, setups = future.result()
            for setup in setups:
                if not _is_on_cooldown(setup):
                    all_setups.append(setup)

    # Sort by confidence score (desc), take top-N only
    all_setups.sort(key=lambda s: s.get("confidence", 0), reverse=True)
    top_setups = all_setups[:max_signals]

    if len(all_setups) > max_signals:
        skipped = len(all_setups) - max_signals
        logger.info(f"[{strat_name}] {len(all_setups)} setup valid → ambil {max_signals} terbaik, skip {skipped} lainnya")

    # BTC bias check — once per scan cycle, used to filter counter-trend paper entries
    btc_bias = "neutral"
    if PAPER_TRADING_ENABLED and _paper_trader is not None:
        btc_bias = _get_btc_bias()

    # Alert policy: only send signal alert if paper_trader actually executes (or schedules pending).
    # Reason: user wants "daging" signals only — no noise for setups that get rejected by
    # daily limit, max positions, BTC bias, CE zone depth, etc.
    # Fallback: if paper trading is OFF, alert ALL valid signals (no filter to gate against).
    signals_sent = 0
    for setup in top_setups:
        sym = setup["symbol"]
        direction = setup["direction"]

        if PAPER_TRADING_ENABLED and _paper_trader is not None:
            # Pre-filter: BTC bias counter-trend
            if btc_bias == "bearish" and direction == "LONG":
                logger.info(f"[ALERT] Skip {sym} LONG: BTC 4H bias bearish — no alert (would-be counter-trend)")
                _mark_cooldown(setup)
                continue
            if btc_bias == "bullish" and direction == "SHORT":
                logger.info(f"[ALERT] Skip {sym} SHORT: BTC 4H bias bullish — no alert (would-be counter-trend)")
                _mark_cooldown(setup)
                continue

            # Try paper open — let paper_trader apply all its filters
            try:
                result = _paper_trader.open_position(setup)
            except Exception as pe:
                logger.error(f"[PAPER] Error open position {sym}: {pe}")
                result = None

            if not result:
                # Paper rejected (daily limit / max positions / CE zone / duplicate / etc) — no alert
                logger.info(f"[ALERT] Skip {sym} {direction}: paper_trader rejected — no alert sent")
                _mark_cooldown(setup)
                continue

            # Paper accepted (open position OR pending limit order) — send signal alert
            sent = _alerter.send_signal_alert(setup)
            _mark_cooldown(setup)
            if sent:
                signals_sent += 1

            # Send paper-specific notification
            status = _paper_trader.get_status()
            result["balance_at_open"] = f"{status['balance']:.2f}"
            if result.get("status") == "pending":
                _alerter.send_paper_pending(result)
            else:
                _alerter.send_paper_entry(result)
        else:
            # Paper trading OFF: alert all valid signals (no execution gate)
            sent = _alerter.send_signal_alert(setup)
            _mark_cooldown(setup)
            if sent:
                signals_sent += 1
            else:
                logger.debug(f"[ALERT] Sinyal {sym} di-skip atau gagal dikirim ke Telegram")

    logger.info(f"=== [{strat_name}] Scan selesai — {signals_sent} sinyal dari {len(pairs)} pair ===")


def scan_swing() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "swing"))


def scan_intraday() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "intraday"))


def scan_scalp() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "scalp"))


def check_paper_positions() -> None:
    """Check open paper trade positions against current prices. Runs every minute.
    Wrapped with 50s hard timeout to prevent API hangs from freezing the scheduler.
    """
    if not PAPER_TRADING_ENABLED or _paper_trader is None:
        return

    def _inner():
        bias = _get_btc_bias()
        exits = _paper_trader.check_positions(_fetcher, market_bias=bias)
        for pos, reason in exits:
            status = _paper_trader.get_status()
            pos["balance_after"] = f"{status['balance']:.2f}"
            if reason == "PENDING_FILLED":
                pos["balance_at_open"] = pos["balance_after"]
                _alerter.send_paper_entry(pos)
            else:
                _alerter.send_paper_exit(pos, reason)

    with ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_inner)
        try:
            future.result(timeout=50)
        except FuturesTimeoutError:
            logger.error("[PAPER] check_positions timed out after 50s — skipping this cycle")
        except Exception as e:
            logger.error(f"[PAPER] Error checking positions: {e}", exc_info=True)


def eod_close_positions() -> None:
    """
    End-of-day close: tutup semua posisi open di 22:00 WIB.
    Memastikan tidak ada posisi yang melewati hari trading berikutnya.
    """
    if not PAPER_TRADING_ENABLED or _paper_trader is None:
        return
    try:
        closed = _paper_trader.close_all_eod(_fetcher)
        for pos, _exit_price in closed:
            _alerter.send_paper_exit(pos, "EXIT_EOD")
        if closed:
            logger.info(f"[PAPER EOD] Ditutup {len(closed)} posisi di akhir hari")
    except Exception as e:
        logger.error(f"[PAPER EOD] Error: {e}", exc_info=True)


def send_daily_summary() -> None:
    """Kirim ringkasan aktivitas trading hari ini ke Telegram. Jalan setiap hari 23:59 WIB."""
    if not PAPER_TRADING_ENABLED or _paper_trader is None:
        return
    try:
        summary = _paper_trader.get_daily_summary()
        _alerter.send_daily_summary(summary)
    except Exception as e:
        logger.error(f"[PAPER] Error daily summary: {e}", exc_info=True)


def send_paper_monthly_recap() -> None:
    """Send paper trading monthly recap. Runs on the 1st of each month at 00:01 WIB."""
    if not PAPER_TRADING_ENABLED or _paper_trader is None:
        return
    from datetime import timedelta
    import pytz as _pytz
    now = __import__("datetime").datetime.now(tz=_pytz.timezone(TIMEZONE))
    # Recap is for the previous month
    last_month = (now.replace(day=1) - timedelta(days=1))
    try:
        stats = _paper_trader.get_monthly_recap(last_month.month, last_month.year)
        _alerter.send_paper_monthly_recap(stats)
    except Exception as e:
        logger.error(f"[PAPER] Error sending monthly recap: {e}", exc_info=True)


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
    global _fetcher, _alerter, _scheduler, _cmd_handler, _paper_trader

    logger.info("=" * 50)
    logger.info("SMC Signal Bot starting up...")
    logger.info("=" * 50)

    _validate_config()
    _load_cooldown()  # restore signal cooldown state from previous run

    # Initialize modules
    _fetcher = DataFetcher()
    _alerter = TelegramAlerter()

    # Initialize paper trader
    if PAPER_TRADING_ENABLED:
        _paper_trader = PaperTrader()
        status = _paper_trader.get_status()
        logger.info(
            f"[PAPER] Paper trading ON — balance=${status['balance']:.2f}, "
            f"open={status['open_positions']}, total closed={status['total_closed']}"
        )
        logger.info(f"[PAPER] Posisi terbuka organik dari scan reguler — maks 3/hari, tutup EOD 22:00")

    # Send startup notification
    all_pairs = sorted(set(p for s in STRATEGIES for p in s["pairs"]))
    _alerter.send_startup_message(all_pairs)

    # Start interactive command handler (handles /price and /porto commands)
    _cmd_handler = TelegramCommandHandler(TELEGRAM_BOT_TOKEN, _fetcher, _paper_trader)
    _cmd_handler.start()

    # Register shutdown handlers
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Configure scheduler
    tz = pytz.timezone(TIMEZONE)
    _scheduler = BlockingScheduler(timezone=tz)
    _scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)

    # One scan job per strategy, each with its own interval
    # Scalp is disabled — only swing and intraday send signals
    strategy_fns = {
        "swing": scan_swing,
        "intraday": scan_intraday,
    }
    for strat in STRATEGIES:
        if strat["name"] not in strategy_fns:
            continue  # scalp (and any future disabled strategy) skipped
        fn = strategy_fns[strat["name"]]
        _scheduler.add_job(
            fn,
            trigger="interval",
            minutes=strat["scan_interval_minutes"],
            id=f"scan_{strat['name']}",
            name=f"{strat['label']} Scanner",
            misfire_grace_time=300,
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

    # Paper trading jobs
    if PAPER_TRADING_ENABLED:
        _scheduler.add_job(
            check_paper_positions,
            trigger="interval",
            minutes=1,          # real-time: cek setiap 1 menit
            id="paper_check",
            name="Paper Position Check",
            misfire_grace_time=30,
            max_instances=1,
        )
        _scheduler.add_job(
            send_daily_summary,
            trigger="cron",
            hour=23,
            minute=59,
            id="daily_summary",
            name="Daily Trading Summary",
            misfire_grace_time=600,
            max_instances=1,
        )
        _scheduler.add_job(
            send_paper_monthly_recap,
            trigger="cron",
            day=1,
            hour=0,
            minute=1,
            id="paper_monthly_recap",
            name="Paper Monthly Recap",
            misfire_grace_time=3600,
            max_instances=1,
        )
        # EOD close: tutup semua posisi terbuka jam 22:00 WIB (tidak ada posisi lintas hari)
        _scheduler.add_job(
            eod_close_positions,
            trigger="cron",
            hour=22,
            minute=0,
            id="paper_eod_close",
            name="Paper EOD Close",
            misfire_grace_time=1800,
            max_instances=1,
        )
        logger.info("  💼 Paper trading: max 3/hari organik, EOD close 22:00 WIB")

    logger.info(f"Watching: {', '.join(TRADING_PAIRS)}")
    logger.info("Bot is running. Press Ctrl+C to stop.")

    # Start health check HTTP server (port 8080 for Railway uptime monitoring)
    _start_health_server(port=8080)

    # Run the first scan immediately — scalp disabled, only swing + intraday
    for fn, name in [(scan_swing, "swing"), (scan_intraday, "intraday"),
                     (send_market_update, "market_update")]:
        try:
            fn()
        except Exception as e:
            logger.error(f"Initial {name} scan failed: {e}", exc_info=True)

    # Start blocking scheduler
    try:
        _scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")


if __name__ == "__main__":
    main()
