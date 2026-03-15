"""
Telegram alert module.
Formats and sends trading signal alerts and market updates to Telegram.
Uses the Telegram Bot API directly via requests (synchronous HTTP) to avoid
asyncio event loop issues when called repeatedly from a scheduler.
"""

import time
from datetime import datetime, timedelta
import pytz
import requests

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE
from utils.logger import get_logger

logger = get_logger(__name__)

_WIB = pytz.timezone(TIMEZONE)
_DEDUP_EXPIRY_HOURS = 1.5


class TelegramAlerter:
    """Sends formatted SMC trading alerts and market updates to Telegram."""

    def __init__(
        self,
        token: str = TELEGRAM_BOT_TOKEN,
        chat_id: str = TELEGRAM_CHAT_ID,
    ):
        if not token or not chat_id:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env"
            )
        self._token = token
        self._chat_id = chat_id
        self._api_url = f"https://api.telegram.org/bot{token}"
        # Dedup: maps (symbol, direction, rounded_entry) → expiry datetime
        self._sent_setups: dict[tuple, datetime] = {}

    # ─── Public API ────────────────────────────────────────────────────────────

    def send_signal_alert(self, setup: dict) -> bool:
        """
        Format and send a trading signal alert.

        Args:
            setup: Canonical setup dict from entry_engine.analyze_pair()

        Returns:
            True if message was sent successfully, False otherwise
        """
        price = setup["entry_mid"]
        if price >= 100:
            rounded_price = round(price, 2)
        elif price >= 10:
            rounded_price = round(price, 3)
        else:
            rounded_price = round(price, 4)
        dedup_key = (
            setup["symbol"],
            setup["direction"],
            setup.get("strategy_name", "unknown"),
            rounded_price,
        )

        if self._is_duplicate(dedup_key):
            logger.info(f"Skipping duplicate alert: {dedup_key}")
            return False

        message = self._format_signal(setup)
        success = self._send(message)

        if success:
            expiry = datetime.now(tz=_WIB) + timedelta(hours=_DEDUP_EXPIRY_HOURS)
            self._sent_setups[dedup_key] = expiry
            logger.info(f"Alert sent: {setup['symbol']} {setup['direction']}")

        return success

    def send_market_update(self, market_data: list[dict]) -> bool:
        """
        Format and send the periodic market update summary.

        Args:
            market_data: List of per-symbol market summary dicts

        Returns:
            True if message was sent successfully
        """
        message = self._format_market_update(market_data)
        return self._send(message)

    def send_paper_entry(self, position: dict) -> bool:
        """Send Telegram notification when a paper trade is opened."""
        is_long = position["direction"] == "LONG"
        emoji = "🟢" if is_long else "🔴"
        dir_label = position["direction"]
        strategy_map = {"swing": "📈 Swing", "intraday": "⏱️ Intraday", "scalp": "⚡ Scalping"}
        strategy_label = strategy_map.get(position.get("strategy", ""), position.get("strategy", ""))

        def fmt(p: float) -> str:
            if p >= 1000:
                return f"${p:,.2f}"
            elif p >= 1:
                return f"${p:.4f}"
            else:
                return f"${p:.6f}"

        entry = position["entry_price"]
        sl = position["sl"]
        sl_pct = abs(entry - sl) / entry * 100
        opened_at = position.get("opened_at", "")
        try:
            ts_str = datetime.fromisoformat(opened_at).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            ts_str = str(opened_at)

        msg = (
            f"💼 *PAPER TRADE ENTRY*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} *{position['symbol']} {dir_label}* — {strategy_label}\n"
            f"💰 Entry: `{fmt(entry)}`\n"
            f"🛑 SL: `{fmt(sl)}` (-{sl_pct:.2f}%)\n"
            f"✅ TP1: `{fmt(position['tp1'])}` | TP2: `{fmt(position['tp2'])}`\n"
            f"📊 Leverage: {position['leverage']}x | Qty: {position['qty']:.5f}\n"
            f"💵 Margin: ${position['margin_used']:.2f} | Risk: ${position['risk_amount']:.2f} ({position['risk_amount']/max(position['margin_used']*position['leverage'],0.01)*100:.0f}%)\n"
            f"⚡ RR: 1:{position['rr']:.1f} | Score: {position['score']}/8\n"
            f"📈 Balance: ${position.get('balance_at_open', '?')}\n"
            f"⏰ {ts_str}"
        )
        return self._send(msg)

    def send_paper_exit(self, position: dict, reason: str) -> bool:
        """Send Telegram notification when a paper trade is closed (or partial TP1)."""
        is_long = position["direction"] == "LONG"
        is_win = position.get("exit_pnl", 0) >= 0
        is_partial = position.get("partial", False)

        if reason == "TP1_HIT":
            status_emoji = "🎯"
            status_label = "TP1 HIT — 50% CLOSED, SL → Breakeven"
        elif reason == "TP2_HIT":
            status_emoji = "✅"
            status_label = "TP2 HIT — FULL CLOSE 🏆"
        elif reason == "SL_HIT_BE":
            status_emoji = "↩️"
            status_label = "SL HIT — Breakeven (modal aman)"
        elif reason == "EXPIRED":
            status_emoji = "⏳"
            status_label = "EXPIRED — Auto Cancel"
        else:
            status_emoji = "❌"
            status_label = "SL HIT — Loss"

        dir_emoji = "🟢" if is_long else "🔴"
        pnl = position.get("exit_pnl", 0)
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        def fmt(p: float) -> str:
            if p >= 1000:
                return f"${p:,.2f}"
            elif p >= 1:
                return f"${p:.4f}"
            else:
                return f"${p:.6f}"

        entry = position["entry_price"]
        exit_price = position.get("exit_price", entry)

        # Duration
        try:
            opened = datetime.fromisoformat(position["opened_at"])
            closed = datetime.fromisoformat(position["exit_at"])
            delta = closed - opened
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            duration_str = f"{hours}j {minutes}m"
        except Exception:
            duration_str = "?"

        try:
            ts_str = datetime.fromisoformat(position["exit_at"]).strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            ts_str = "?"

        balance_after = position.get("balance_after", "?")
        strategy_map = {"swing": "📈 Swing", "intraday": "⏱️ Intraday", "scalp": "⚡ Scalping"}
        strategy_label = strategy_map.get(position.get("strategy", ""), position.get("strategy", ""))

        # For TP1 partial: note that position is still half-open
        note_line = ""
        if reason == "TP1_HIT":
            note_line = f"\n📌 _Sisa 50% masih open, SL dipindah ke entry (breakeven)_"
        elif reason == "EXPIRED":
            max_h = {"scalp": 6, "intraday": 24, "swing": 72}.get(position.get("strategy", "intraday"), 24)
            note_line = f"\n📌 _Posisi ditutup otomatis setelah {max_h}j tanpa TP/SL hit_"

        msg = (
            f"📤 *PAPER TRADE EXIT*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{dir_emoji} *{position['symbol']} {position['direction']}* [{strategy_label}]\n"
            f"{status_emoji} {status_label}\n"
            f"💰 Entry: `{fmt(entry)}` → Exit: `{fmt(exit_price)}`\n"
            f"{pnl_emoji} P&L: *{pnl_sign}${pnl:.2f}*\n"
            f"📊 Leverage: {position['leverage']}x | Durasi: {duration_str}\n"
            f"💵 Balance: ${balance_after}{note_line}\n"
            f"⏰ {ts_str}"
        )
        return self._send(msg)

    def send_paper_monthly_recap(self, stats: dict) -> bool:
        """Send monthly portfolio recap on the 1st of each month."""
        import calendar
        month_name = calendar.month_name[stats["month"]]
        year = stats["year"]

        balance = stats["current_balance"]
        initial = stats["initial_balance"]
        roi = stats["roi"]
        roi_sign = "+" if roi >= 0 else ""
        roi_emoji = "📈" if roi >= 0 else "📉"

        total = stats["total_trades"]
        win_rate = stats["win_rate"]
        pnl = stats["total_pnl"]
        pnl_sign = "+" if pnl >= 0 else ""
        dd = stats["max_drawdown"]

        best = stats.get("best_trade")
        worst = stats.get("worst_trade")
        best_str = f"{best['symbol']} {best['direction']} {'+' if best.get('exit_pnl',0)>=0 else ''}{best.get('exit_pnl',0):.2f}$" if best else "N/A"
        worst_str = f"{worst['symbol']} {worst['direction']} {'+' if worst.get('exit_pnl',0)>=0 else ''}{worst.get('exit_pnl',0):.2f}$" if worst else "N/A"

        best_strat = stats.get("best_strategy")
        strat_str = f"{best_strat[0].capitalize()} ({best_strat[1]} trade, {best_strat[2]:.0f}% WR)" if best_strat else "N/A"

        msg = (
            f"📊 *PAPER TRADE RECAP — {month_name} {year}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: *${balance:.2f}* (dari ${initial:.2f})\n"
            f"{roi_emoji} ROI: *{roi_sign}{roi:.2f}%*\n"
            f"\n"
            f"📋 *Statistik Bulan Ini:*\n"
            f"• Total Trade: {total}\n"
            f"• Menang: {stats['wins']} ({win_rate:.1f}%)\n"
            f"• Kalah: {stats['losses']}\n"
            f"• P&L Bulan Ini: *{pnl_sign}${pnl:.2f}*\n"
            f"• Trade Terbaik: {best_str}\n"
            f"• Trade Terburuk: {worst_str}\n"
            f"• Max Drawdown: -{dd:.1f}%\n"
            f"\n"
            f"🏆 Strategi Terbaik: {strat_str}\n"
            f"📂 Posisi Aktif: {stats['open_positions']}\n"
            f"⏰ {datetime.now(tz=_WIB).strftime('%d %B %Y %H:%M %Z')}"
        )
        return self._send(msg)

    def send_forced_scan_start(self, slot_target: int, today_count: int, needed: int) -> bool:
        """Notif saat forced scan dimulai — user tahu bot sedang aktif mencari setup."""
        slot_map = {1: "09:00", 2: "13:00", 3: "18:00"}
        slot_time = slot_map.get(slot_target, f"Slot {slot_target}")
        msg = (
            f"🔍 *FORCED SCAN — {slot_time} WIB*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Trade hari ini: {today_count} | Target: {slot_target}\n"
            f"Butuh: *{needed} posisi lagi*\n"
            f"Scanning 10 pair utama..."
        )
        return self._send(msg)

    def send_forced_scan_result(self, slot_target: int, opened: int, needed: int, balance: float) -> bool:
        """Notif hasil forced scan — berhasil buka berapa posisi."""
        if opened >= needed:
            status_emoji = "✅"
            status_label = f"Berhasil buka *{opened}* posisi"
        elif opened > 0:
            status_emoji = "⚠️"
            status_label = f"Buka *{opened}/{needed}* posisi (tidak penuh)"
        else:
            status_emoji = "❌"
            status_label = "Tidak ada setup valid ditemukan"
        msg = (
            f"{status_emoji} *FORCED SCAN — Selesai*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{status_label}\n"
            f"💼 Balance: *${balance:.2f}*\n"
            f"⏰ {datetime.now(tz=_WIB).strftime('%H:%M WIB')}"
        )
        return self._send(msg)

    def send_daily_summary(self, summary: dict) -> bool:
        """Kirim ringkasan aktivitas trading harian jam 23:59 WIB."""
        balance = summary["balance"]
        initial = summary["initial_balance"]
        roi = (balance - initial) / initial * 100 if initial > 0 else 0.0
        roi_sign = "+" if roi >= 0 else ""
        roi_emoji = "📈" if roi >= 0 else "📉"

        opened_today = summary["total_opened_today"]
        closed_today = summary["total_closed_today"]
        still_open = summary["open_positions"]

        msg = (
            f"📊 *RINGKASAN HARIAN — {summary['date']}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💼 Balance: *${balance:.2f}*\n"
            f"{roi_emoji} ROI: *{roi_sign}{roi:.2f}%* (dari ${initial:.2f})\n"
            f"\n"
            f"📋 *Trade Hari Ini:*\n"
            f"• Total dibuka: {opened_today} posisi\n"
            f"• Closed: {closed_today} posisi\n"
            f"• Masih open: {still_open} posisi\n"
            f"• Total closed semua waktu: {summary['total_closed']}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏰ 23:59 WIB"
        )
        return self._send(msg)

    def send_startup_message(self, pairs: list[str]) -> bool:
        """Send bot startup notification."""
        msg = (
            f"🤖 *SMC Signal Bot Started* v2.1\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 {len(pairs)} pairs dipantau (parallel scan)\n"
            f"✅ Range trading mode: ON (dead zone 40-60%)\n"
            f"✅ Scalp/Intraday: score 2/8 min, Swing: 3/8\n"
            f"✅ TF conflict: 1 TF boleh beda (soft check)\n"
            f"🔫 Forced trade: 09:00, 13:00, 18:00 WIB\n"
            f"⏰ {datetime.now(tz=_WIB).strftime('%Y-%m-%d %H:%M %Z')}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Bot aktif dan siap mengirim signal!"
        )
        return self._send(msg)

    # ─── Formatters ────────────────────────────────────────────────────────────

    def _format_signal(self, setup: dict) -> str:
        """Format a trading setup into a Telegram message string."""
        is_long = setup["direction"] == "LONG"
        emoji = "🟢" if is_long else "🔴"
        dir_label = "LONG" if is_long else "SHORT"

        confidence = setup["confidence"]
        max_score = 8
        stars = "⭐" * min(confidence, max_score) + "☆" * max(0, max_score - confidence)

        reasons_text = "\n".join(f"• {r}" for r in setup["reasons"])

        ts = setup["timestamp"]
        if isinstance(ts, datetime):
            ts_str = ts.strftime("%Y-%m-%d %H:%M %Z")
        else:
            ts_str = str(ts)

        # Format numbers based on price magnitude
        def fmt(price: float) -> str:
            if price >= 1000:
                return f"${price:,.2f}"
            elif price >= 1:
                return f"${price:.4f}"
            else:
                return f"${price:.6f}"

        strategy_emoji = setup.get("strategy_emoji", "📊")
        strategy_label = setup.get("strategy_label", "")

        msg = (
            f"{emoji} *{setup['symbol']} {dir_label}* {strategy_emoji} {strategy_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Entry: `{fmt(setup['entry_low'])} - {fmt(setup['entry_high'])}`\n"
            f"🛑 SL: `{fmt(setup['sl'])}`\n"
            f"✅ TP1: `{fmt(setup['tp1'])}` TP2: `{fmt(setup['tp2'])}`\n"
            f"📐 RR: *1:{setup['rr_tp2']:.1f}* {stars} {confidence}/{max_score}\n"
            f"⚡ Leverage rekomendasi: 10x\n"
            f"📈 Trend: {setup['htf_trend']}\n"
            f"📋 {reasons_text}\n"
            f"⏰ {ts_str}"
        )
        return msg

    def _format_market_update(self, market_data: list[dict]) -> str:
        """Format periodic market update for all pairs."""
        now_str = datetime.now(tz=_WIB).strftime("%Y-%m-%d %H:%M %Z")

        lines = [
            f"📊 *MARKET UPDATE - {now_str}*",
            "━━━━━━━━━━━━━━━━━━━━",
            "",
        ]

        for item in market_data:
            symbol = item.get("symbol", "???")
            price = item.get("current_price", 0)
            change = item.get("price_change_pct", 0)
            trend_1d = item.get("trend_1d", "?").capitalize()
            trend_4h = item.get("trend_4h", "?").capitalize()
            support = item.get("key_support")
            resistance = item.get("key_resistance")

            change_sign = "+" if change >= 0 else ""
            change_emoji = "📈" if change >= 0 else "📉"

            def fmt(p):
                return f"${p:,.2f}" if p >= 100 else f"${p:.4f}"

            support_str = fmt(support) if support else "N/A"
            resistance_str = fmt(resistance) if resistance else "N/A"

            lines += [
                f"*{symbol}:* {fmt(price)} ({change_sign}{change:.2f}%) {change_emoji}",
                f"  Daily: {trend_1d} | 4H: {trend_4h}",
                f"  Key Level: Support {support_str}, Resistance {resistance_str}",
                "",
            ]

        active_setups = sum(1 for item in market_data if item.get("has_setup", False))
        lines += [
            f"Active Setups: {active_setups}",
            "━━━━━━━━━━━━━━━━━━━━",
        ]

        return "\n".join(lines)

    # ─── Internal helpers ──────────────────────────────────────────────────────

    def _send(self, message: str, retries: int = 3) -> bool:
        """
        Send a Telegram message with retry logic.

        Args:
            message: Markdown-formatted message text
            retries: Number of retry attempts

        Returns:
            True on success, False after all retries fail
        """
        payload = {
            "chat_id": self._chat_id,
            "text": message,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(
                    f"{self._api_url}/sendMessage",
                    json=payload,
                    timeout=30,
                )
                resp.raise_for_status()
                return True
            except requests.HTTPError as e:
                logger.warning(f"Telegram HTTP error (attempt {attempt}/{retries}): {e} — {resp.text[:200]}")
            except requests.RequestException as e:
                logger.warning(f"Telegram send failed (attempt {attempt}/{retries}): {e}")
            except Exception as e:
                logger.error(f"Unexpected error sending Telegram message: {e}")
                return False
            if attempt < retries:
                time.sleep(5 * attempt)

        logger.error("All Telegram send attempts failed")
        return False

    def _is_duplicate(self, key: tuple) -> bool:
        """Check deduplication cache, removing expired entries."""
        now = datetime.now(tz=_WIB)
        # Clean up expired entries
        expired = [k for k, exp in self._sent_setups.items() if exp < now]
        for k in expired:
            del self._sent_setups[k]

        return key in self._sent_setups
