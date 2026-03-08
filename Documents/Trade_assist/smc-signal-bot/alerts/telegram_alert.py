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
_DEDUP_EXPIRY_HOURS = 4


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
        dedup_key = (
            setup["symbol"],
            setup["direction"],
            round(setup["entry_mid"], 1),
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

    def send_startup_message(self, pairs: list[str]) -> bool:
        """Send bot startup notification."""
        pairs_str = ", ".join(pairs)
        msg = (
            f"🤖 *SMC Signal Bot Started*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Scanning: `{pairs_str}`\n"
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
        max_score = 7
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
            f"{emoji} *{setup['symbol']} {dir_label}* {strategy_emoji} [{strategy_label}]\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 Entry: `{fmt(setup['entry_low'])} - {fmt(setup['entry_high'])}`\n"
            f"🛑 SL: `{fmt(setup['sl'])}`\n"
            f"✅ TP1: `{fmt(setup['tp1'])}` | TP2: `{fmt(setup['tp2'])}`\n"
            f"📐 RR: *1:{setup['rr_tp2']:.1f}* | {stars} {confidence}/7\n"
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
