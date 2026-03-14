"""
Paper Trading engine.
Simulates virtual trade entries/exits based on signals from entry_engine.
Tracks P&L, win rate, and portfolio performance from a $100 starting balance.

State is persisted to paper_state.json so it survives bot restarts.
"""

import json
import uuid
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytz

from config.settings import (
    PAPER_INITIAL_BALANCE,
    PAPER_LEVERAGE,
    PAPER_RISK_PCT,
    PAPER_MAX_POSITIONS,
    PAPER_MAX_MARGIN_PCT,
    PAPER_STATE_FILE,
    TIMEZONE,
)
from utils.logger import get_logger

logger = get_logger(__name__)

_WIB = pytz.timezone(TIMEZONE)

# Reason codes for exit notifications
EXIT_TP1 = "TP1_HIT"
EXIT_TP2 = "TP2_HIT"
EXIT_SL = "SL_HIT"
EXIT_SL_BE = "SL_HIT_BE"  # SL hit after breakeven move


class PaperTrader:
    """
    Simulates paper trading from signals produced by entry_engine.

    Capital management rules:
    - Risk 5% of current balance per trade
    - Max 3 concurrent open positions
    - Max 25% of balance as margin per single position
    - Leverage: 10x fixed
    - TP1 hit: close 50% of position, move SL to breakeven
    - TP2 hit: close remaining 50%
    - SL hit: close 100% (or remaining after partial TP1)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state_path = Path(PAPER_STATE_FILE)
        self._state = self._load_state()
        logger.info(
            f"[PAPER] Initialized — balance=${self._state['balance']:.2f}, "
            f"open={len(self._state['open_positions'])} positions"
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def open_position(self, setup: dict) -> dict | None:
        """
        Try to open a paper trade based on a signal setup dict.

        Args:
            setup: Canonical setup dict from entry_engine.analyze_pair()

        Returns:
            Position dict if opened, None if skipped (max positions, duplicate, etc.)
        """
        with self._lock:
            symbol = setup["symbol"]
            direction = setup["direction"]
            entry = float(setup["entry_mid"])
            sl = float(setup["sl"])
            tp1 = float(setup["tp1"])
            tp2 = float(setup["tp2"])
            strategy = setup.get("strategy_name", "unknown")
            score = setup.get("confidence", 0)
            rr = setup.get("rr_tp2", 0.0)

            open_positions = self._state["open_positions"]

            # Guard: max concurrent positions
            if len(open_positions) >= PAPER_MAX_POSITIONS:
                logger.info(f"[PAPER] Skip {symbol} {direction}: max {PAPER_MAX_POSITIONS} positions reached")
                return None

            # Guard: no duplicate symbol+direction
            for pos in open_positions:
                if pos["symbol"] == symbol and pos["direction"] == direction:
                    logger.info(f"[PAPER] Skip {symbol} {direction}: position already open")
                    return None

            balance = self._state["balance"]

            # Position sizing
            sl_dist_pct = abs(entry - sl) / entry
            if sl_dist_pct < 0.0001:
                logger.warning(f"[PAPER] Skip {symbol}: SL distance too small ({sl_dist_pct:.4%})")
                return None

            risk_amount = balance * PAPER_RISK_PCT
            # notional = risk / (sl_pct × leverage)  → so that sl_pct × leverage × notional = risk
            position_notional = risk_amount / (sl_dist_pct * PAPER_LEVERAGE)
            margin_used = position_notional / PAPER_LEVERAGE

            # Cap margin to PAPER_MAX_MARGIN_PCT of balance
            max_margin = balance * PAPER_MAX_MARGIN_PCT
            if margin_used > max_margin:
                margin_used = max_margin
                position_notional = margin_used * PAPER_LEVERAGE
                # Recalculate actual risk at capped size
                risk_amount = position_notional * sl_dist_pct * PAPER_LEVERAGE

            qty = position_notional / entry

            now = datetime.now(tz=_WIB)
            position = {
                "id": str(uuid.uuid4())[:8],
                "symbol": symbol,
                "direction": direction,
                "entry_price": round(entry, 6),
                "sl": round(sl, 6),
                "sl_original": round(sl, 6),
                "tp1": round(tp1, 6),
                "tp2": round(tp2, 6),
                "qty": round(qty, 6),
                "qty_remaining": round(qty, 6),
                "position_notional": round(position_notional, 4),
                "margin_used": round(margin_used, 4),
                "leverage": PAPER_LEVERAGE,
                "risk_amount": round(risk_amount, 4),
                "strategy": strategy,
                "score": score,
                "rr": round(rr, 2),
                "opened_at": now.isoformat(),
                "tp1_hit": False,
                "sl_moved_to_be": False,
                "realized_pnl": 0.0,
                "status": "open",
            }

            open_positions.append(position)
            self._save_state()
            logger.info(
                f"[PAPER] Opened {symbol} {direction} @ {entry:.4f} | "
                f"margin=${margin_used:.2f}, risk=${risk_amount:.2f}, "
                f"qty={qty:.6f}, strategy={strategy}"
            )
            return position

    def check_positions(self, fetcher) -> list[tuple[dict, str]]:
        """
        Check all open positions against current market prices.

        Args:
            fetcher: DataFetcher instance (uses get_current_price)

        Returns:
            List of (closed_position_dict, exit_reason) tuples for each closed position.
        """
        exits = []

        with self._lock:
            still_open = []
            for pos in self._state["open_positions"]:
                symbol = pos["symbol"]
                perp_sym = f"{symbol.replace('/USDT', '')}/USDT:USDT"

                try:
                    current_price = fetcher.get_current_price(perp_sym)
                except Exception as e:
                    logger.warning(f"[PAPER] Could not fetch price for {symbol}: {e}")
                    still_open.append(pos)
                    continue

                if current_price is None:
                    still_open.append(pos)
                    continue

                is_long = pos["direction"] == "LONG"
                tp1 = pos["tp1"]
                tp2 = pos["tp2"]
                sl = pos["sl"]
                entry = pos["entry_price"]
                qty_remaining = pos["qty_remaining"]
                position_notional = pos["position_notional"]
                # Remaining notional (after potential partial close at TP1)
                fraction_remaining = qty_remaining / pos["qty"] if pos["qty"] > 0 else 1.0
                remaining_notional = position_notional * fraction_remaining

                closed_now = False
                exit_reason = None

                # ── TP1 check (first target, partial close) ──
                if not pos["tp1_hit"]:
                    tp1_hit = (is_long and current_price >= tp1) or (not is_long and current_price <= tp1)
                    if tp1_hit:
                        # Partial close: 50% of position
                        half_notional = position_notional * 0.5
                        pnl_partial = self._calc_pnl(is_long, entry, tp1, half_notional)
                        pos["realized_pnl"] += pnl_partial
                        pos["qty_remaining"] = round(pos["qty"] * 0.5, 6)
                        pos["tp1_hit"] = True
                        pos["sl"] = entry  # move SL to breakeven
                        pos["sl_moved_to_be"] = True
                        self._state["balance"] += pnl_partial
                        self._update_peak()

                        logger.info(
                            f"[PAPER] {symbol} TP1 hit @ {tp1:.4f} | "
                            f"+${pnl_partial:.2f} partial, SL moved to BE={entry:.4f}"
                        )
                        # Emit a TP1 partial exit notification
                        pos_snapshot = dict(pos)
                        pos_snapshot["exit_price"] = tp1
                        pos_snapshot["exit_pnl"] = pnl_partial
                        pos_snapshot["exit_at"] = datetime.now(tz=_WIB).isoformat()
                        pos_snapshot["partial"] = True
                        exits.append((pos_snapshot, EXIT_TP1))

                # ── TP2 / SL check (full close of remaining) ──
                if not closed_now and pos["tp1_hit"]:
                    tp2_hit = (is_long and current_price >= tp2) or (not is_long and current_price <= tp2)
                    sl_hit = (is_long and current_price <= sl) or (not is_long and current_price >= sl)

                    if tp2_hit:
                        pnl_final = self._calc_pnl(is_long, entry, tp2, remaining_notional)
                        exit_reason = EXIT_TP2
                        exit_price = tp2
                    elif sl_hit:
                        pnl_final = self._calc_pnl(is_long, entry, sl, remaining_notional)
                        exit_reason = EXIT_SL_BE  # SL at breakeven
                        exit_price = sl
                    else:
                        pnl_final = None
                        exit_price = None

                    if pnl_final is not None:
                        pos["realized_pnl"] += pnl_final
                        pos["status"] = "closed"
                        pos["exit_price"] = exit_price
                        pos["exit_pnl"] = pos["realized_pnl"]
                        pos["exit_at"] = datetime.now(tz=_WIB).isoformat()
                        pos["partial"] = False
                        self._state["balance"] += pnl_final
                        self._update_peak()
                        self._state["closed_positions"].append(pos)
                        exits.append((dict(pos), exit_reason))
                        closed_now = True
                        logger.info(
                            f"[PAPER] {symbol} {exit_reason} @ {exit_price:.4f} | "
                            f"total P&L=${pos['realized_pnl']:.2f}, balance=${self._state['balance']:.2f}"
                        )

                elif not closed_now and not pos["tp1_hit"]:
                    # Full position: check TP2 and SL
                    tp2_hit = (is_long and current_price >= tp2) or (not is_long and current_price <= tp2)
                    sl_hit = (is_long and current_price <= sl) or (not is_long and current_price >= sl)

                    if tp2_hit:
                        pnl = self._calc_pnl(is_long, entry, tp2, position_notional)
                        exit_reason = EXIT_TP2
                        exit_price = tp2
                    elif sl_hit:
                        pnl = self._calc_pnl(is_long, entry, sl, position_notional)
                        exit_reason = EXIT_SL
                        exit_price = sl
                    else:
                        pnl = None
                        exit_price = None

                    if pnl is not None:
                        pos["realized_pnl"] = pnl
                        pos["status"] = "closed"
                        pos["exit_price"] = exit_price
                        pos["exit_pnl"] = pnl
                        pos["exit_at"] = datetime.now(tz=_WIB).isoformat()
                        pos["partial"] = False
                        self._state["balance"] += pnl
                        self._update_peak()
                        self._state["closed_positions"].append(pos)
                        exits.append((dict(pos), exit_reason))
                        closed_now = True
                        logger.info(
                            f"[PAPER] {symbol} {exit_reason} @ {exit_price:.4f} | "
                            f"P&L=${pnl:.2f}, balance=${self._state['balance']:.2f}"
                        )

                if not closed_now:
                    still_open.append(pos)

            self._state["open_positions"] = still_open
            if exits:
                self._save_state()

        return exits

    def get_monthly_recap(self, month: int, year: int) -> dict:
        """
        Calculate paper trading statistics for a given month.

        Args:
            month: Month number (1-12)
            year: Full year (e.g. 2026)

        Returns:
            Stats dict with balance, trades, win_rate, P&L, drawdown, etc.
        """
        with self._lock:
            closed = self._state["closed_positions"]

        # Filter to target month
        monthly = []
        for pos in closed:
            try:
                exit_dt = datetime.fromisoformat(pos["exit_at"])
                if exit_dt.month == month and exit_dt.year == year:
                    monthly.append(pos)
            except Exception:
                continue

        total_trades = len(monthly)
        wins = sum(1 for p in monthly if p.get("exit_pnl", 0) > 0)
        losses = total_trades - wins
        total_pnl = sum(p.get("exit_pnl", 0) for p in monthly)
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0.0

        best = max(monthly, key=lambda p: p.get("exit_pnl", 0), default=None)
        worst = min(monthly, key=lambda p: p.get("exit_pnl", 0), default=None)

        # Strategy breakdown
        strategy_stats: dict[str, dict] = {}
        for p in monthly:
            s = p.get("strategy", "unknown")
            if s not in strategy_stats:
                strategy_stats[s] = {"trades": 0, "wins": 0}
            strategy_stats[s]["trades"] += 1
            if p.get("exit_pnl", 0) > 0:
                strategy_stats[s]["wins"] += 1

        best_strategy = None
        best_wr = -1.0
        for s, st in strategy_stats.items():
            wr = st["wins"] / st["trades"] * 100 if st["trades"] > 0 else 0
            if wr > best_wr:
                best_wr = wr
                best_strategy = (s, st["trades"], wr)

        with self._lock:
            current_balance = self._state["balance"]
            initial = self._state["initial_balance"]
            peak = self._state["peak_balance"]

        roi = (current_balance - initial) / initial * 100 if initial > 0 else 0.0
        # Max drawdown: from peak to current (simplified)
        max_dd = (peak - current_balance) / peak * 100 if peak > 0 else 0.0

        return {
            "month": month,
            "year": year,
            "current_balance": round(current_balance, 2),
            "initial_balance": round(initial, 2),
            "roi": round(roi, 2),
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "best_trade": best,
            "worst_trade": worst,
            "max_drawdown": round(max_dd, 2),
            "best_strategy": best_strategy,
            "open_positions": len(self._state["open_positions"]),
        }

    def get_status(self) -> dict:
        """Return current portfolio status (for startup message or on-demand)."""
        with self._lock:
            return {
                "balance": round(self._state["balance"], 2),
                "initial_balance": round(self._state["initial_balance"], 2),
                "open_positions": len(self._state["open_positions"]),
                "total_closed": len(self._state["closed_positions"]),
                "peak_balance": round(self._state["peak_balance"], 2),
            }

    def get_today_count(self) -> int:
        """Hitung posisi yang dibuka hari ini (open + closed combined)."""
        today = datetime.now(tz=_WIB).date()
        count = 0
        with self._lock:
            for pos in self._state["open_positions"] + self._state["closed_positions"]:
                try:
                    opened_dt = datetime.fromisoformat(pos["opened_at"])
                    if not opened_dt.tzinfo:
                        opened_dt = _WIB.localize(opened_dt)
                    if opened_dt.astimezone(_WIB).date() == today:
                        count += 1
                except Exception:
                    pass
        return count

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _calc_pnl(is_long: bool, entry: float, exit_price: float, notional: float) -> float:
        """Calculate realized P&L for a position using leverage."""
        price_change_pct = (exit_price - entry) / entry
        if not is_long:
            price_change_pct = -price_change_pct
        pnl = price_change_pct * notional * PAPER_LEVERAGE
        return round(pnl, 4)

    def _update_peak(self) -> None:
        """Update peak balance tracker (for drawdown calculation)."""
        if self._state["balance"] > self._state["peak_balance"]:
            self._state["peak_balance"] = self._state["balance"]

    def _load_state(self) -> dict:
        """Load state from JSON file, or create a fresh state if file doesn't exist."""
        if self._state_path.exists():
            try:
                with open(self._state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                # Ensure all required keys exist (backwards compatibility)
                state.setdefault("initial_balance", PAPER_INITIAL_BALANCE)
                state.setdefault("peak_balance", state.get("balance", PAPER_INITIAL_BALANCE))
                state.setdefault("open_positions", [])
                state.setdefault("closed_positions", [])
                logger.info(f"[PAPER] State loaded from {self._state_path}")
                return state
            except Exception as e:
                logger.warning(f"[PAPER] Could not load state file ({e}), starting fresh")

        return {
            "balance": PAPER_INITIAL_BALANCE,
            "initial_balance": PAPER_INITIAL_BALANCE,
            "peak_balance": PAPER_INITIAL_BALANCE,
            "open_positions": [],
            "closed_positions": [],
        }

    def _save_state(self) -> None:
        """Persist state to JSON file (called inside lock)."""
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[PAPER] Failed to save state: {e}")
