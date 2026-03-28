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
    PAPER_MAX_POSITIONS,
    PAPER_MAX_DAILY_TRADES,
    PAPER_MAX_PENDING,
    PAPER_MARGIN_PER_TRADE_PCT,
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
EXIT_EXPIRED = "EXPIRED"  # Position auto-closed after max duration

# Max open duration per strategy before auto-cancel
# Swing uses 4H OBs that need 1-2 days to play out — keep positions alive longer
# Intraday uses 1H OBs — 12h is enough for realistic simulation
_MAX_DURATION_HOURS: dict[str, int] = {
    "scalp": 4,
    "intraday": 12,  # 6h → 12h: intraday OBs need up to half a day
    "swing": 36,     # 8h → 36h: swing positions can survive overnight (4H OBs need time)
}

# Pending limit order expiry per strategy
# Approaching signals add pending orders that expire if price never touches zone
_PENDING_EXPIRY_HOURS: dict[str, int] = {
    "scalp": 1,
    "intraday": 2,
    "swing": 6,
}


class PaperTrader:
    """
    Simulates paper trading from signals produced by entry_engine.

    Capital management rules:
    - Risk 10% of current balance per trade
    - Max 3 trades per day (hard daily cap — tidak buka posisi ke-4)
    - Max 2 posisi bersamaan (concurrent)
    - Max 30% of balance as margin per single position
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
            f"open={len(self._state['open_positions'])} positions | "
            f"max {PAPER_MAX_DAILY_TRADES} trades/day, {PAPER_MAX_POSITIONS} concurrent, "
            f"10x leverage, margin={int(PAPER_MARGIN_PER_TRADE_PCT*100)}%/trade"
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    def open_position(self, setup: dict) -> dict | None:
        """
        Try to open a paper trade based on a signal setup dict.

        Entry hanya dibuka jika harga pasar saat ini berada di dalam zona OB/FVG.
        Tidak ada forced entry — posisi terbuka organik dari scan reguler.

        Returns:
            Position dict if opened, None if skipped (max positions, duplicate, stale zone)
        """
        with self._lock:
            symbol = setup["symbol"]
            direction = setup["direction"]
            current_price = float(setup["current_price"])   # actual market price at signal time
            entry_low = float(setup["entry_low"])
            entry_high = float(setup["entry_high"])
            # Use full OB zone for zone check (signal fires when price in full zone)
            # entry_low/entry_high are CE (narrowed half-zone) — too restrictive for entry
            zone_low = float(setup.get("ob_zone_low", entry_low))
            zone_high = float(setup.get("ob_zone_high", entry_high))
            sl = float(setup["sl"])
            tp1 = float(setup["tp1"])
            tp2 = float(setup["tp2"])
            strategy = setup.get("strategy_name", "unknown")
            score = setup.get("confidence", 0)
            rr = setup.get("rr_tp2", 0.0)

            open_positions = self._state["open_positions"]

            # Guard: max daily trades (hard cap — 3 trades per day total, open or closed)
            today_count = self._count_today_trades()
            if today_count >= PAPER_MAX_DAILY_TRADES:
                logger.info(
                    f"[PAPER] Skip {symbol} {direction}: daily limit reached "
                    f"({today_count}/{PAPER_MAX_DAILY_TRADES} trades today)"
                )
                return None

            # Guard: max concurrent positions
            if len(open_positions) >= PAPER_MAX_POSITIONS:
                logger.info(f"[PAPER] Skip {symbol} {direction}: max {PAPER_MAX_POSITIONS} concurrent positions")
                return None

            # Guard: no duplicate symbol+direction
            for pos in open_positions:
                if pos["symbol"] == symbol and pos["direction"] == direction:
                    logger.info(f"[PAPER] Skip {symbol} {direction}: position already open")
                    return None

            # Approaching signal: add as pending limit order at zone boundary
            # This simulates placing a limit order BEFORE price enters the zone.
            # The order fills when price actually touches the zone boundary.
            if setup.get("approaching", False):
                return self._add_pending_order(setup)

            is_long = direction == "LONG"

            # Guard: harga harus di dalam CE zone (half OB — optimal entry area)
            # CE zone = upper half of OB for LONG, lower half for SHORT (SMC standard)
            # LONG CE:  entry_low (ob_midpoint) → entry_high (ob_zone_high)
            # SHORT CE: entry_low (ob_zone_low)  → entry_high (ob_midpoint)
            # Jika price sudah terlalu dalam ke OB (past midpoint), SL tinggal sedikit
            # → entry suboptimal, skip dan tunggu setup baru
            ce_low  = float(setup.get("entry_low",  zone_low))
            ce_high = float(setup.get("entry_high", zone_high))

            if current_price < ce_low or current_price > ce_high:
                zone_width = zone_high - zone_low if zone_high != zone_low else 1
                depth_pct = (
                    (ce_low - current_price) / zone_width * 100
                    if is_long
                    else (current_price - ce_high) / zone_width * 100
                )
                logger.info(
                    f"[PAPER] Skip {symbol} {direction}: price {current_price:.4f} "
                    f"past CE zone [{ce_low:.4f}-{ce_high:.4f}] "
                    f"({depth_pct:.0f}% terlalu dalam ke OB — SL terlalu dekat)"
                )
                return None

            # Guard: full zone check (price must still be inside OB at all)
            if current_price < zone_low or current_price > zone_high:
                logger.info(
                    f"[PAPER] Skip {symbol} {direction}: price {current_price:.4f} "
                    f"outside full zone [{zone_low:.4f}-{zone_high:.4f}] — signal stale"
                )
                return None

            # Fill at current market price — now guaranteed to be in CE zone (optimal half)
            entry = current_price

            # Guard: TP1/TP2 must be on the correct side of entry
            # (if TP1 ≤ entry for LONG or TP1 ≥ entry for SHORT, the setup is degenerate)
            if is_long and (tp1 <= entry or tp2 <= entry):
                logger.info(f"[PAPER] Skip {symbol} LONG: TP {tp1:.4f}/{tp2:.4f} ≤ entry {entry:.4f}")
                return None
            if not is_long and (tp1 >= entry or tp2 >= entry):
                logger.info(f"[PAPER] Skip {symbol} SHORT: TP {tp1:.4f}/{tp2:.4f} ≥ entry {entry:.4f}")
                return None

            balance = self._state["balance"]

            # Position sizing — fixed allocation: 1/3 balance per trade
            sl_dist_pct = abs(entry - sl) / entry
            if sl_dist_pct < 0.0001:
                logger.warning(f"[PAPER] Skip {symbol}: SL distance too small ({sl_dist_pct:.4%})")
                return None

            # Fixed margin = 33% of balance per position → meaningful size
            # With 10x leverage: $33 margin → $330 exposure
            margin_used = balance * PAPER_MARGIN_PER_TRADE_PCT
            margin_used = min(margin_used, balance * 0.50)        # hard cap 50% balance
            position_size_usd = margin_used * PAPER_LEVERAGE      # total exposure
            risk_amount = position_size_usd * sl_dist_pct         # actual $ risk
            qty = position_size_usd / entry

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
                "position_size_usd": round(position_size_usd, 4),
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
                f"margin=${margin_used:.2f}, pos=${position_size_usd:.2f}, "
                f"risk=${risk_amount:.2f}, qty={qty:.6f}, strategy={strategy}"
            )
            return position

    def check_positions(self, fetcher) -> list[tuple[dict, str]]:
        """
        Check all open positions against current market prices.
        Also checks pending limit orders for fills.

        Args:
            fetcher: DataFetcher instance (uses get_current_price)

        Returns:
            List of (closed_position_dict, exit_reason) tuples for each closed position.
            Filled pending orders are returned with reason "PENDING_FILLED".
        """
        exits = []

        # Check pending limit orders first — convert filled ones to active positions
        filled_from_pending = self.check_pending_orders(fetcher)
        for pos in filled_from_pending:
            exits.append((dict(pos), "PENDING_FILLED"))

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
                position_size_usd = pos.get("position_size_usd") or pos.get("position_notional", 0) * PAPER_LEVERAGE
                # Remaining position size (after potential partial close at TP1)
                fraction_remaining = qty_remaining / pos["qty"] if pos["qty"] > 0 else 1.0
                remaining_size_usd = position_size_usd * fraction_remaining

                closed_now = False
                exit_reason = None

                # ── Expiry check (auto-cancel jika terlalu lama) ──
                try:
                    opened_dt = datetime.fromisoformat(pos["opened_at"])
                    if not opened_dt.tzinfo:
                        opened_dt = _WIB.localize(opened_dt)
                    age_hours = (datetime.now(tz=_WIB) - opened_dt).total_seconds() / 3600
                    max_hours = _MAX_DURATION_HOURS.get(pos.get("strategy", "intraday"), 24)
                    if age_hours >= max_hours:
                        pnl_exp = self._calc_pnl(is_long, entry, current_price, remaining_size_usd)
                        pos["realized_pnl"] += pnl_exp
                        pos["status"] = "closed"
                        pos["exit_price"] = current_price
                        pos["exit_pnl"] = pos["realized_pnl"]
                        pos["exit_at"] = datetime.now(tz=_WIB).isoformat()
                        pos["partial"] = False
                        self._state["balance"] += pnl_exp
                        self._update_peak()
                        self._state["closed_positions"].append(pos)
                        exits.append((dict(pos), EXIT_EXPIRED))
                        closed_now = True
                        logger.info(
                            f"[PAPER] {symbol} EXPIRED after {age_hours:.1f}h @ {current_price:.4f} | "
                            f"P&L=${pos['realized_pnl']:.2f}"
                        )
                except Exception as exp_e:
                    logger.warning(f"[PAPER] Expiry check error for {symbol}: {exp_e}")

                # ── TP1 check (first target, partial close) ──
                if not pos["tp1_hit"]:
                    tp1_hit = (is_long and current_price >= tp1) or (not is_long and current_price <= tp1)
                    if tp1_hit:
                        # Partial close: 50% of position
                        pnl_partial = self._calc_pnl(is_long, entry, tp1, position_size_usd * 0.5)
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
                        pnl_final = self._calc_pnl(is_long, entry, tp2, remaining_size_usd)
                        exit_reason = EXIT_TP2
                        exit_price = tp2
                    elif sl_hit:
                        pnl_final = self._calc_pnl(is_long, entry, sl, remaining_size_usd)
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
                        pnl = self._calc_pnl(is_long, entry, tp2, position_size_usd)
                        exit_reason = EXIT_TP2
                        exit_price = tp2
                    elif sl_hit:
                        pnl = self._calc_pnl(is_long, entry, sl, position_size_usd)
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
        with self._lock:
            return self._count_today_trades()

    def close_all_eod(self, fetcher) -> list[tuple[dict, float]]:
        """
        Tutup posisi intraday/scalp di akhir hari (22:00 WIB).
        Swing positions TIDAK ditutup — dibiarkan hidup hingga max_duration 36h
        karena 4H OB butuh waktu bermain, termasuk overnight.

        Returns:
            List of (closed_position_dict, exit_price) for each position closed.
        """
        closed_list = []
        with self._lock:
            # Cancel all pending orders at EOD (no carryover to next day)
            cancelled = len(self._state.get("pending_orders", []))
            if cancelled:
                logger.info(f"[PAPER EOD] Cancelled {cancelled} pending limit orders")
            self._state["pending_orders"] = []

            still_open = []
            for pos in self._state["open_positions"]:
                # Swing positions survive overnight — close naturally via max_duration (36h)
                if pos.get("strategy") == "swing":
                    still_open.append(pos)
                    continue
                symbol = pos["symbol"]
                perp_sym = f"{symbol.replace('/USDT', '')}/USDT:USDT"
                try:
                    current_price = fetcher.get_current_price(perp_sym)
                except Exception:
                    current_price = None

                if current_price is None:
                    still_open.append(pos)
                    logger.warning(f"[PAPER EOD] {symbol}: gagal ambil harga, posisi tetap open")
                    continue

                is_long = pos["direction"] == "LONG"
                entry = pos["entry_price"]
                qty_remaining = pos.get("qty_remaining", pos["qty"])
                position_size_usd = pos.get("position_size_usd", 0)
                fraction_remaining = qty_remaining / pos["qty"] if pos["qty"] > 0 else 1.0
                remaining_size_usd = position_size_usd * fraction_remaining

                pnl = self._calc_pnl(is_long, entry, current_price, remaining_size_usd)
                pos["realized_pnl"] = pos.get("realized_pnl", 0.0) + pnl
                pos["status"] = "closed"
                pos["exit_price"] = current_price
                pos["exit_pnl"] = pos["realized_pnl"]
                pos["exit_at"] = datetime.now(tz=_WIB).isoformat()
                pos["exit_reason"] = "EXIT_EOD"
                pos["partial"] = False
                self._state["balance"] += pnl
                self._state["closed_positions"].append(pos)
                closed_list.append((pos, current_price))
                logger.info(
                    f"[PAPER EOD] Closed {symbol} {pos['direction']} @ {current_price:.4f} | "
                    f"PnL=${pnl:.2f}"
                )

            self._state["open_positions"] = still_open
            self._save_state()
        return closed_list

    def get_strategy_stats(self, days: int | None = None) -> dict:
        """
        Per-strategy WR breakdown.

        Args:
            days: Lookback window in days. None = all time.

        Returns:
            Dict keyed by strategy name with WR stats.
        """
        with self._lock:
            closed = list(self._state["closed_positions"])

        now = datetime.now(tz=_WIB)
        cutoff = now - timedelta(days=days) if days else None

        stats: dict[str, dict] = {}
        for pos in closed:
            if cutoff:
                try:
                    exit_dt = datetime.fromisoformat(pos.get("exit_at", pos["opened_at"]))
                    if not exit_dt.tzinfo:
                        exit_dt = _WIB.localize(exit_dt)
                    if exit_dt < cutoff:
                        continue
                except Exception:
                    continue

            strat = pos.get("strategy", "unknown")
            if strat not in stats:
                stats[strat] = {
                    "total": 0, "wins": 0, "losses": 0,
                    "pnl": 0.0,
                    "tp2": 0, "tp1": 0, "sl": 0, "sl_be": 0,
                    "expired": 0, "eod": 0,
                }

            s = stats[strat]
            s["total"] += 1
            pnl = pos.get("exit_pnl", 0) or 0.0
            s["pnl"] += pnl

            reason = pos.get("exit_reason", "") or ""
            if "TP2" in reason:
                s["tp2"] += 1
            elif "TP1" in reason:
                s["tp1"] += 1
            elif reason == "SL_HIT_BE" or "BE" in reason:
                s["sl_be"] += 1
            elif "SL" in reason:
                s["sl"] += 1
            elif "EOD" in reason:
                s["eod"] += 1
            elif "EXPIRED" in reason:
                s["expired"] += 1

            if pnl > 0:
                s["wins"] += 1
            else:
                s["losses"] += 1

        for s in stats.values():
            s["win_rate"] = round(s["wins"] / s["total"] * 100, 1) if s["total"] > 0 else 0.0
            s["pnl"] = round(s["pnl"], 2)

        return stats

    def get_daily_summary(self) -> dict:
        """Ambil statistik trading hari ini untuk daily summary Telegram notification."""
        today = datetime.now(tz=_WIB).date()
        with self._lock:
            all_positions = self._state["open_positions"] + self._state["closed_positions"]
            opened_today = []
            for pos in all_positions:
                try:
                    opened_dt = datetime.fromisoformat(pos["opened_at"])
                    if not opened_dt.tzinfo:
                        opened_dt = _WIB.localize(opened_dt)
                    if opened_dt.astimezone(_WIB).date() == today:
                        opened_today.append(pos)
                except Exception:
                    pass

            closed_today = []
            for pos in self._state["closed_positions"]:
                try:
                    closed_dt = datetime.fromisoformat(pos.get("closed_at", pos["opened_at"]))
                    if not closed_dt.tzinfo:
                        closed_dt = _WIB.localize(closed_dt)
                    if closed_dt.astimezone(_WIB).date() == today:
                        closed_today.append(pos)
                except Exception:
                    pass

            return {
                "date": today.strftime("%d %b %Y"),
                "balance": round(self._state["balance"], 2),
                "initial_balance": round(self._state["initial_balance"], 2),
                "total_opened_today": len(opened_today),
                "total_closed_today": len(closed_today),
                "open_positions": len(self._state["open_positions"]),
                "total_closed": len(self._state["closed_positions"]),
                "strategy_stats_7d": self.get_strategy_stats(days=7),
            }

    # ── Pending limit order management ────────────────────────────────────────

    def _add_pending_order(self, setup: dict) -> dict | None:
        """
        Add a pending limit order for an approaching signal.

        The fill price is the zone boundary — where price enters the zone first:
        - LONG approaching (price falling toward bullish OB): fill at zone_high
        - SHORT approaching (price rising toward bearish OB): fill at zone_low

        Returns the pending order dict (with status='pending'), or None if not added.
        """
        symbol = setup["symbol"]
        direction = setup["direction"]
        is_long = direction == "LONG"
        zone_low = float(setup.get("ob_zone_low", 0))
        zone_high = float(setup.get("ob_zone_high", 0))

        if not zone_low or not zone_high:
            logger.info(f"[PAPER] Skip pending {symbol}: no ob_zone in setup")
            return None

        pending_orders = self._state.setdefault("pending_orders", [])

        # Guard: max pending orders
        if len(pending_orders) >= PAPER_MAX_PENDING:
            logger.info(f"[PAPER] Skip pending {symbol} {direction}: max {PAPER_MAX_PENDING} pending orders")
            return None

        # Guard: no duplicate symbol+direction in pending
        for p in pending_orders:
            if p["symbol"] == symbol and p["direction"] == direction:
                logger.info(f"[PAPER] Skip pending {symbol} {direction}: already in queue")
                return None

        # Guard: no duplicate in open positions
        for pos in self._state["open_positions"]:
            if pos["symbol"] == symbol and pos["direction"] == direction:
                return None

        # Fill price = zone boundary where price enters first
        fill_price = zone_high if is_long else zone_low

        strategy = setup.get("strategy_name", "intraday")
        expiry_hours = _PENDING_EXPIRY_HOURS.get(strategy, 2)
        now = datetime.now(tz=_WIB)

        order = {
            "id": str(uuid.uuid4())[:8],
            "symbol": symbol,
            "direction": direction,
            "fill_price": round(fill_price, 6),
            "ob_zone_low": round(zone_low, 6),
            "ob_zone_high": round(zone_high, 6),
            "sl": round(float(setup["sl"]), 6),
            "tp1": round(float(setup["tp1"]), 6),
            "tp2": round(float(setup["tp2"]), 6),
            "strategy": strategy,
            "score": setup.get("confidence", 0),
            "rr": round(float(setup.get("rr_tp2", 0.0)), 2),
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=expiry_hours)).isoformat(),
            "status": "pending",
        }

        pending_orders.append(order)
        self._save_state()

        logger.info(
            f"[PAPER] Pending LIMIT added: {symbol} {direction} @ {fill_price:.4f} "
            f"(expires {expiry_hours}h)"
        )
        return order

    def check_pending_orders(self, fetcher) -> list[dict]:
        """
        Check if any pending limit orders have been filled by current market price.

        Returns list of newly opened positions (from filled pending orders).
        Called from check_positions() BEFORE the open position loop.
        """
        filled_positions = []

        with self._lock:
            now = datetime.now(tz=_WIB)
            still_pending = []

            for order in self._state.get("pending_orders", []):
                symbol = order["symbol"]
                direction = order["direction"]
                is_long = direction == "LONG"
                fill_price = order["fill_price"]

                # Check expiry
                try:
                    expires_dt = datetime.fromisoformat(order["expires_at"])
                    if not expires_dt.tzinfo:
                        expires_dt = _WIB.localize(expires_dt)
                    if now >= expires_dt:
                        logger.info(f"[PAPER] Pending {symbol} {direction} EXPIRED unfilled")
                        continue  # drop expired order
                except Exception:
                    pass

                # Fetch current price
                perp_sym = f"{symbol.replace('/USDT', '')}/USDT:USDT"
                try:
                    current_price = fetcher.get_current_price(perp_sym)
                except Exception as e:
                    logger.warning(f"[PAPER] Pending {symbol}: price fetch error: {e}")
                    still_pending.append(order)
                    continue

                if current_price is None:
                    still_pending.append(order)
                    continue

                # Check fill condition
                # LONG: price must DROP to fill_price (zone_high)
                # SHORT: price must RISE to fill_price (zone_low)
                filled = (
                    (is_long and current_price <= fill_price) or
                    (not is_long and current_price >= fill_price)
                )

                if not filled:
                    still_pending.append(order)
                    continue

                # Check if we can still open (concurrent limit)
                if len(self._state["open_positions"]) >= PAPER_MAX_POSITIONS:
                    logger.info(
                        f"[PAPER] Pending {symbol} {direction} filled but max positions reached — cancelling"
                    )
                    continue  # cancel this pending order

                # Check daily trade limit
                today_count = self._count_today_trades()
                if today_count >= PAPER_MAX_DAILY_TRADES:
                    logger.info(
                        f"[PAPER] Pending {symbol} filled but daily limit reached — cancelling"
                    )
                    continue

                # Check duplicate in open positions
                dup = any(
                    p["symbol"] == symbol and p["direction"] == direction
                    for p in self._state["open_positions"]
                )
                if dup:
                    continue

                # Open position from pending order
                position = self._open_from_pending(order, fill_price)
                if position:
                    filled_positions.append(position)

            self._state["pending_orders"] = still_pending
            if filled_positions:
                self._save_state()

        return filled_positions

    def _open_from_pending(self, order: dict, fill_price: float) -> dict | None:
        """
        Convert a filled pending limit order into an active position.
        fill_price is the zone boundary where the limit order triggered.
        Called inside lock from check_pending_orders().
        """
        symbol = order["symbol"]
        direction = order["direction"]
        is_long = direction == "LONG"

        entry = fill_price
        sl = order["sl"]
        tp1 = order["tp1"]
        tp2 = order["tp2"]

        # Validate TP is still on the correct side of fill price
        if is_long and (tp1 <= entry or tp2 <= entry):
            logger.info(
                f"[PAPER] Pending {symbol} LONG: TP {tp1:.4f}/{tp2:.4f} ≤ fill {entry:.4f} — invalid"
            )
            return None
        if not is_long and (tp1 >= entry or tp2 >= entry):
            logger.info(
                f"[PAPER] Pending {symbol} SHORT: TP {tp1:.4f}/{tp2:.4f} ≥ fill {entry:.4f} — invalid"
            )
            return None

        balance = self._state["balance"]
        sl_dist_pct = abs(entry - sl) / entry
        if sl_dist_pct < 0.0001:
            logger.warning(f"[PAPER] Pending {symbol}: SL distance too small")
            return None

        margin_used = balance * PAPER_MARGIN_PER_TRADE_PCT
        margin_used = min(margin_used, balance * 0.50)
        position_size_usd = margin_used * PAPER_LEVERAGE
        risk_amount = position_size_usd * sl_dist_pct
        qty = position_size_usd / entry

        now = datetime.now(tz=_WIB)
        position = {
            "id": order["id"],
            "symbol": symbol,
            "direction": direction,
            "entry_price": round(entry, 6),
            "sl": round(sl, 6),
            "sl_original": round(sl, 6),
            "tp1": round(tp1, 6),
            "tp2": round(tp2, 6),
            "qty": round(qty, 6),
            "qty_remaining": round(qty, 6),
            "position_size_usd": round(position_size_usd, 4),
            "margin_used": round(margin_used, 4),
            "leverage": PAPER_LEVERAGE,
            "risk_amount": round(risk_amount, 4),
            "strategy": order.get("strategy", "unknown"),
            "score": order.get("score", 0),
            "rr": round(order.get("rr", 0.0), 2),
            "opened_at": now.isoformat(),
            "tp1_hit": False,
            "sl_moved_to_be": False,
            "realized_pnl": 0.0,
            "status": "open",
            "from_pending": True,
        }

        self._state["open_positions"].append(position)
        logger.info(
            f"[PAPER] Pending FILLED → {symbol} {direction} @ {entry:.4f} | "
            f"margin=${margin_used:.2f}, pos=${position_size_usd:.2f}, risk=${risk_amount:.2f}"
        )
        return position

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _count_today_trades(self) -> int:
        """Count trades opened today (open + closed). Called inside lock."""
        today = datetime.now(tz=_WIB).date()
        count = 0
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

    @staticmethod
    def _calc_pnl(is_long: bool, entry: float, exit_price: float, position_size_usd: float) -> float:
        """Calculate realized P&L.
        position_size_usd = margin × leverage (total exposure).
        P&L = price_change% × position_size. No extra leverage factor needed.
        """
        price_change_pct = (exit_price - entry) / entry
        if not is_long:
            price_change_pct = -price_change_pct
        return round(price_change_pct * position_size_usd, 4)

    def _update_peak(self) -> None:
        """Update peak balance tracker (for drawdown calculation)."""
        if self._state["balance"] > self._state["peak_balance"]:
            self._state["peak_balance"] = self._state["balance"]

    def _load_state(self) -> dict:
        """Load state from JSON file, or create a fresh state if file doesn't exist.

        On load, any open position older than its max_duration is force-closed at
        its own entry price (wash trade) to prevent stale/yesterday prices persisting.
        """
        if self._state_path.exists():
            try:
                with open(self._state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                state.setdefault("initial_balance", PAPER_INITIAL_BALANCE)
                state.setdefault("peak_balance", state.get("balance", PAPER_INITIAL_BALANCE))
                state.setdefault("open_positions", [])
                state.setdefault("closed_positions", [])
                state.setdefault("pending_orders", [])

                # Purge stale open positions (older than their max_duration)
                now = datetime.now(tz=_WIB)
                fresh, purged = [], []
                for pos in state["open_positions"]:
                    try:
                        opened_dt = datetime.fromisoformat(pos["opened_at"])
                        if not opened_dt.tzinfo:
                            opened_dt = _WIB.localize(opened_dt)
                        age_h = (now - opened_dt).total_seconds() / 3600
                        max_h = _MAX_DURATION_HOURS.get(pos.get("strategy", "intraday"), 24)
                        if age_h >= max_h:
                            purged.append(pos["symbol"])
                        else:
                            fresh.append(pos)
                    except Exception:
                        fresh.append(pos)
                if purged:
                    logger.info(f"[PAPER] Startup purge: {len(purged)} expired positions removed: {purged}")
                state["open_positions"] = fresh

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
            "pending_orders": [],
        }

    def _save_state(self) -> None:
        """Persist state to JSON file (called inside lock)."""
        try:
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"[PAPER] Failed to save state: {e}")
