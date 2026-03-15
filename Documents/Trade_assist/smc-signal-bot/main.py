"""
SMC Signal Bot - Main entry point.
Initializes all modules and runs the APScheduler for periodic scanning.
"""

import sys
import signal
import logging
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler

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
    PAPER_FORCE_PAIRS,
    PAPER_MAX_POSITIONS,
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
_forced_intraday_strategy: dict | None = None  # intraday with relaxed thresholds for forced scan

# ── Signal cooldown tracking ────────────────────────────────────────────────────
# Prevents the same pair+direction from spamming every scan cycle.
# Key: "{symbol}_{direction}_{strategy_name}", Value: datetime last sent
_signal_cooldown: dict[str, datetime] = {}
_signal_cooldown_lock = threading.Lock()
_COOLDOWN_HOURS: dict[str, int] = {
    "swing": 8,      # swing signals valid 8 hours
    "intraday": 4,   # intraday signals valid 4 hours
    "scalp": 2,      # scalp signals valid 2 hours
}


def _is_on_cooldown(setup: dict) -> bool:
    """Return True if this pair+direction was sent recently (any strategy counts).

    Cooldown is keyed by symbol+direction only — prevents cross-strategy duplicates
    (e.g., scalp AND intraday both firing LTC LONG in the same window).
    Cooldown duration is determined by the strategy that sent the signal first.
    """
    key = f"{setup['symbol']}_{setup['direction']}"
    with _signal_cooldown_lock:
        last_sent, _ = _signal_cooldown.get(key, (None, None))
        if not last_sent:
            return False
        hours = _COOLDOWN_HOURS.get(setup.get("strategy_name", ""), 4)
        return datetime.now() - last_sent < timedelta(hours=hours)


def _mark_cooldown(setup: dict) -> None:
    """Record that this signal was just sent (keyed by symbol+direction only)."""
    key = f"{setup['symbol']}_{setup['direction']}"
    with _signal_cooldown_lock:
        _signal_cooldown[key] = (datetime.now(), setup.get("strategy_name", ""))


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


def _scan_strategy(strategy: dict) -> None:
    """Scan all pairs for a given strategy in parallel (4 workers)."""
    pairs = strategy["pairs"]
    perp_pairs = [f"{p}:USDT" for p in pairs]
    strat_name = strategy["name"].upper()
    logger.info(f"=== [{strat_name}] Scan dimulai ({len(pairs)} pair, parallel) ===")

    signals_sent = 0
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="scan") as executor:
        futures = {
            executor.submit(_scan_pair, sym, perp, strategy): sym
            for sym, perp in zip(pairs, perp_pairs)
        }
        for future in as_completed(futures):
            sym, setups = future.result()
            for setup in setups:
                if _is_on_cooldown(setup):
                    logger.debug(
                        f"[COOLDOWN] {sym} {setup['direction']} ({strategy['name']}) — cooldown aktif, skip"
                    )
                    continue
                sent = _alerter.send_signal_alert(setup)
                if not sent:
                    logger.warning(f"[ALERT] Gagal kirim sinyal {sym} ke Telegram")
                else:
                    _mark_cooldown(setup)
                    signals_sent += 1
                if PAPER_TRADING_ENABLED and _paper_trader is not None:
                    try:
                        position = _paper_trader.open_position(setup)
                        if position:
                            status = _paper_trader.get_status()
                            position["balance_at_open"] = f"{status['balance']:.2f}"
                            _alerter.send_paper_entry(position)
                    except Exception as pe:
                        logger.error(f"[PAPER] Error open position {sym}: {pe}")

    logger.info(f"=== [{strat_name}] Scan selesai — {signals_sent} sinyal dari {len(pairs)} pair ===")


def scan_swing() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "swing"))


def scan_intraday() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "intraday"))


def scan_scalp() -> None:
    _scan_strategy(next(s for s in STRATEGIES if s["name"] == "scalp"))


def check_paper_positions() -> None:
    """Check open paper trade positions against current prices. Runs every 5 minutes."""
    if not PAPER_TRADING_ENABLED or _paper_trader is None:
        return
    try:
        exits = _paper_trader.check_positions(_fetcher)
        for pos, reason in exits:
            # Attach balance after exit for notification
            status = _paper_trader.get_status()
            pos["balance_after"] = f"{status['balance']:.2f}"
            _alerter.send_paper_exit(pos, reason)
    except Exception as e:
        logger.error(f"[PAPER] Error checking positions: {e}", exc_info=True)


def _run_forced_scan_tier(strategy: dict, needed: int, tier_label: str) -> int:
    """Scan PAPER_FORCE_PAIRS dengan strategy tier tertentu. Return jumlah posisi dibuka."""
    opened = 0
    for pair in PAPER_FORCE_PAIRS:
        if opened >= needed:
            break
        if len(_paper_trader._state["open_positions"]) >= PAPER_MAX_POSITIONS:
            logger.info("[PAPER FORCED] Max posisi tercapai, berhenti")
            break
        try:
            perp = f"{pair.replace('/USDT', '')}/USDT:USDT"
            mtf_data = _fetcher.fetch_for_strategy(perp, strategy)
            if any(df is None for df in mtf_data.values()):
                logger.warning(f"[PAPER FORCED] [{tier_label}] {pair}: data incomplete — skip")
                continue
            setups = entry_engine.analyze_pair(pair, mtf_data, strategy)
            for setup in setups:
                position = _paper_trader.open_position(setup)
                if position:
                    status = _paper_trader.get_status()
                    position["balance_at_open"] = f"{status['balance']:.2f}"
                    _alerter.send_paper_entry(position)
                    opened += 1
                    logger.info(f"[PAPER FORCED] [{tier_label}] Buka {pair} {setup['direction']}")
                    break  # 1 posisi per pair
        except Exception as e:
            logger.error(f"[PAPER FORCED] [{tier_label}] Error {pair}: {e}", exc_info=True)
    return opened


def forced_paper_scan(slot_target: int) -> None:
    """
    Wajib scan intraday di 10 pair utama jika trade hari ini < slot_target.
    Dipanggil 3x sehari (09:00, 13:00, 18:00 WIB) — wajib ada trade setiap hari.
    Tier 1: conf=2, rr=1.2. Tier 2 fallback: conf=1, rr=1.0 — tidak ada hari tanpa posisi.
    """
    if not PAPER_TRADING_ENABLED or _paper_trader is None or _forced_intraday_strategy is None:
        return

    today_count = _paper_trader.get_today_count()
    needed = slot_target - today_count
    if needed <= 0:
        logger.info(f"[PAPER FORCED] Slot {slot_target}: sudah {today_count} trade hari ini ✓")
        return

    logger.info(f"[PAPER FORCED] Slot {slot_target}: butuh {needed} trade lagi (ada {today_count})")
    _alerter.send_forced_scan_start(slot_target, today_count, needed)

    opened = 0

    # ── Tier 1: threshold normal forced (htf=4h, conf=2, rr=1.2) ─────────────
    opened += _run_forced_scan_tier(_forced_intraday_strategy, needed, "TIER-1")

    # ── Tier 2 fallback: jika tier 1 kurang — conf=1, rr=1.0 ─────────────────
    still_needed = needed - opened
    if still_needed > 0:
        logger.info(f"[PAPER FORCED] TIER-1 kurang ({opened}/{needed}) — coba TIER-2")
        _tier2 = {
            **_forced_intraday_strategy,
            "min_confidence": 1,
            "min_rr": 1.0,
        }
        opened += _run_forced_scan_tier(_tier2, still_needed, "TIER-2")

    # ── Notif hasil ke Telegram ───────────────────────────────────────────────
    status = _paper_trader.get_status()
    _alerter.send_forced_scan_result(slot_target, opened, needed, status["balance"])
    logger.info(f"[PAPER FORCED] Slot {slot_target} selesai — buka {opened} posisi baru")


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
        # Build forced intraday strategy (relaxed thresholds for mandatory daily trading)
        _intraday_base = next(s for s in STRATEGIES if s["name"] == "intraday")
        global _forced_intraday_strategy
        _forced_intraday_strategy = {
            **_intraday_base,
            "pairs": PAPER_FORCE_PAIRS,
            "htf_tfs": ["4h"],     # hanya 1 TF untuk arah — bypass TF conflict 1h/15m
            "min_confidence": 2,   # lebih longgar dari normal (3)
            "min_rr": 1.2,         # lebih longgar dari normal (1.5)
            "ob_fresh_lookback": 60,
        }
        logger.info(f"[PAPER] Forced intraday strategy ready — htf=4h only, min_conf=2, min_rr=1.2")

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
        # Forced intraday scan: 3 slot per hari (WAJIB 3 trade/hari di 10 pair utama)
        for slot_hour, slot_target, slot_id in [
            (9,  1, "paper_force_1"),   # 09:00 WIB → target ≥1 trade hari ini
            (13, 2, "paper_force_2"),   # 13:00 WIB → target ≥2 trade hari ini
            (18, 3, "paper_force_3"),   # 18:00 WIB → target ≥3 trade hari ini
        ]:
            _tgt = slot_target  # capture for lambda closure
            _scheduler.add_job(
                lambda t=_tgt: forced_paper_scan(t),
                trigger="cron",
                hour=slot_hour,
                minute=0,
                id=slot_id,
                name=f"Forced Paper Scan Slot {slot_target}",
                misfire_grace_time=1800,
                max_instances=1,
            )
        logger.info("  💼 Paper trading: position check every 5m, recap every 1st of month")
        logger.info("  🔫 Forced intraday: 09:00 (≥1), 13:00 (≥2), 18:00 (≥3) trade/hari")

    logger.info(f"Watching: {', '.join(TRADING_PAIRS)}")
    logger.info("Bot is running. Press Ctrl+C to stop.")

    # Start health check HTTP server (port 8080 for Railway uptime monitoring)
    _start_health_server(port=8080)

    # Run the first scan immediately — each isolated so one failure won't block others
    for fn, name in [(scan_swing, "swing"), (scan_intraday, "intraday"),
                     (scan_scalp, "scalp"), (send_market_update, "market_update")]:
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
