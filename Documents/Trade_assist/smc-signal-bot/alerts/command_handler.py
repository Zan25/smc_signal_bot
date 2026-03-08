"""
Telegram command handler — polls for user commands and responds interactively.
Runs in a background daemon thread alongside the APScheduler.

Supported commands:
  /price          — inline keyboard to pick a pair
  /price BTC      — direct price check for a specific pair
"""

import time
import threading
import requests
from datetime import datetime
import pytz

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TIMEZONE, STRATEGIES
from utils.logger import get_logger

logger = get_logger(__name__)
_WIB = pytz.timezone(TIMEZONE)


class TelegramCommandHandler:
    """Polls Telegram getUpdates and handles /price commands with inline keyboard."""

    def __init__(self, token: str, fetcher):
        self._token = token
        self._api = f"https://api.telegram.org/bot{token}"
        self._fetcher = fetcher
        self._offset: int | None = None
        self._running = False

        # All unique pairs across all strategies (preserves order)
        self._all_pairs: list[str] = list(dict.fromkeys(
            p for s in STRATEGIES for p in s["pairs"]
        ))

    def start(self) -> None:
        """Start the polling loop in a background daemon thread."""
        self._running = True
        t = threading.Thread(target=self._poll_loop, daemon=True, name="TgPoller")
        t.start()
        logger.info(f"Command handler started — watching {len(self._all_pairs)} pairs")

    def stop(self) -> None:
        self._running = False

    # ── Polling loop ───────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = self._get_updates()
                for upd in updates:
                    try:
                        self._handle_update(upd)
                    except Exception as e:
                        logger.warning(f"Error handling update: {e}")
                    self._offset = upd["update_id"] + 1
            except Exception as e:
                logger.warning(f"Polling error: {e}")
                time.sleep(5)

    def _get_updates(self) -> list:
        params: dict = {
            "timeout": 20,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        try:
            resp = requests.get(f"{self._api}/getUpdates", params=params, timeout=25)
            return resp.json().get("result", [])
        except Exception:
            return []

    # ── Update dispatch ────────────────────────────────────────────────────────

    def _handle_update(self, update: dict) -> None:
        if "message" in update:
            self._handle_message(update["message"])
        elif "callback_query" in update:
            self._handle_callback(update["callback_query"])

    def _handle_message(self, msg: dict) -> None:
        text: str = msg.get("text", "").strip()
        chat_id: int = msg["chat"]["id"]

        if not text.startswith("/price"):
            return

        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            # /price — show pair picker keyboard
            self._send_keyboard(chat_id)
        else:
            # /price BTC or /price BTC/USDT
            query = parts[1].upper().replace("USDT", "").replace("/", "").strip()
            matched = next(
                (p for p in self._all_pairs if p.upper().startswith(query)),
                None,
            )
            if matched:
                self._send_price(chat_id, matched)
            else:
                self._send(chat_id, f"Pair '{parts[1]}' tidak ditemukan.\nKetik /price untuk lihat semua pair.")

    def _handle_callback(self, cb: dict) -> None:
        data: str = cb.get("data", "")
        if not data.startswith("price:"):
            return

        pair = data[len("price:"):]
        chat_id: int = cb["message"]["chat"]["id"]

        self._send_price(chat_id, pair)

        # Remove the loading spinner on the button
        try:
            requests.post(
                f"{self._api}/answerCallbackQuery",
                json={"callback_query_id": cb["id"]},
                timeout=5,
            )
        except Exception:
            pass

    # ── Inline keyboard ────────────────────────────────────────────────────────

    def _send_keyboard(self, chat_id: int) -> None:
        """Send a message with one inline button per pair (3 per row)."""
        keyboard = []
        row: list = []
        for pair in self._all_pairs:
            base = pair.split("/")[0]  # "BTC/USDT" → "BTC"
            row.append({"text": base, "callback_data": f"price:{pair}"})
            if len(row) == 3:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        payload = {
            "chat_id": chat_id,
            "text": "Pilih pair untuk cek harga:",
            "reply_markup": {"inline_keyboard": keyboard},
        }
        try:
            requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.warning(f"Failed to send keyboard: {e}")

    # ── Price fetch & reply ────────────────────────────────────────────────────

    def _send_price(self, chat_id: int, pair: str) -> None:
        """Fetch current price + 24h change and send reply."""
        perp_sym = f"{pair}:USDT"
        try:
            df_15m = self._fetcher.fetch_ohlcv(perp_sym, "15m", limit=2)
            df_1d  = self._fetcher.fetch_ohlcv(perp_sym, "1d",  limit=2)

            if df_15m is None or len(df_15m) == 0:
                self._send(chat_id, f"Gagal mengambil data untuk {pair}.")
                return

            current = float(df_15m["close"].iloc[-1])

            # Format price based on magnitude
            if current >= 1000:
                price_str = f"${current:,.2f}"
            elif current >= 1:
                price_str = f"${current:.4f}"
            else:
                price_str = f"${current:.6f}"

            # 24h change
            change_line = ""
            if df_1d is not None and len(df_1d) >= 2:
                prev = float(df_1d["close"].iloc[-2])
                if prev > 0:
                    chg = (current - prev) / prev * 100
                    sign = "+" if chg >= 0 else ""
                    arrow = "📈" if chg >= 0 else "📉"
                    change_line = f"\n24h: *{sign}{chg:.2f}%* {arrow}"

            now_str = datetime.now(tz=_WIB).strftime("%H:%M WIB")
            msg = (
                f"💰 *{pair}*\n"
                f"Harga: *{price_str}*"
                f"{change_line}\n"
                f"⏰ {now_str}"
            )
            self._send(chat_id, msg)

        except Exception as e:
            logger.error(f"Price fetch error for {pair}: {e}")
            self._send(chat_id, f"Error saat fetch {pair}: {e}")

    def _send(self, chat_id: int, text: str) -> None:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        try:
            requests.post(f"{self._api}/sendMessage", json=payload, timeout=10)
        except Exception as e:
            logger.warning(f"Failed to send message: {e}")
