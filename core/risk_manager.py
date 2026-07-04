"""
core/risk_manager.py
All risk enforcement lives here. The strategy generates entry signals;
this module decides whether to act on them and manages the trade while
it's open.

Key changes from the previous version:
  - ATR-based position sizing: qty = (capital × risk_pct) / (ATR × stop_mult)
  - Per-trade stop loss and take-profit tracked in-memory
  - check_open_trade() called every bar to enforce stop/target
    without waiting for the strategy to signal an exit
  - Consecutive loss counter — after N losses, pause new entries
  - max_trades_per_day cap
  - Kill-switch accounts for unrealized P&L, not just realized
    (a large open loss should also trigger the kill-switch)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
from config.settings import RiskConfig
from core.strategy import Signal, Action


@dataclass
class OrderIntent:
    action: Action
    quantity: int
    stop_price: float = 0.0     # ATR-based stop for this specific order
    target_price: float = 0.0   # ATR-based target


@dataclass
class OpenTrade:
    symbol: str
    entry_price: float
    quantity: int
    direction: int          # +1 = long, -1 = short
    stop_price: float
    target_price: float


class RiskManager:
    def __init__(self, config: RiskConfig, logger):
        self._cfg = config
        self._log = logger

        # Session state
        self.realized_pnl_today: float = 0.0
        self.trades_today: int = 0
        self.consecutive_losses: int = 0
        self._kill_switch: bool = False
        self._open_trade: Optional[OpenTrade] = None

        # Legacy compat — portfolio also tracks position qty, but
        # RiskManager needs its own view for stop/target logic.
        self.current_position_qty: int = 0

    # ── Entry evaluation ─────────────────────────────────────────────────────

    def evaluate(self, signal: Signal, last_price: float, atr: float) -> Optional[OrderIntent]:
        """
        Decide whether to act on a strategy signal. Returns an OrderIntent
        or None (veto). Must be called with the current ATR so sizing is
        correct at the moment of entry.
        """
        if self._kill_switch:
            self._log.warning("KILL SWITCH active — all new entries blocked")
            return None

        # Daily loss limit — check both realized and approximate unrealized
        effective_pnl = self._effective_pnl(last_price)
        loss_limit = min(
            self._cfg.max_daily_loss,
            self._cfg.account_capital * self._cfg.max_daily_loss_pct / 100.0,
        )
        if effective_pnl <= -loss_limit:
            self._log.error(
                "Daily loss limit hit: effective_pnl=%.2f <= -%.2f — engaging kill switch",
                effective_pnl, loss_limit,
            )
            self._kill_switch = True
            return None

        if signal.action == Action.HOLD:
            return None

        if signal.action == Action.EXIT:
            return self._build_exit_intent()

        # Don't enter new trades if already in a position
        if self._open_trade is not None:
            self._log.debug("BUY signal ignored — already in a trade for %s", self._open_trade.symbol)
            return None

        # Max trades per day
        if self.trades_today >= self._cfg.max_trades_per_day:
            self._log.info(
                "Max trades/day (%d) reached — no new entries", self._cfg.max_trades_per_day
            )
            return None

        # Consecutive loss pause
        if self.consecutive_losses >= self._cfg.max_consecutive_losses:
            self._log.warning(
                "%d consecutive losses — pausing new entries for remainder of session",
                self.consecutive_losses,
            )
            return None

        # Short guard
        if signal.action == Action.SELL and not self._cfg.allow_shorts:
            self._log.debug("SELL signal ignored — allow_shorts=False")
            return None

        # ATR validity
        if atr <= 0 or atr != atr:  # nan check
            self._log.warning("ATR invalid (%.4f) — cannot size position, skipping", atr)
            return None

        # ── ATR-based sizing ─────────────────────────────────────────────
        stop_distance = atr * self._cfg.atr_stop_multiplier
        target_distance = atr * self._cfg.atr_target_multiplier

        rr = target_distance / stop_distance
        if rr < self._cfg.min_risk_reward:
            self._log.info(
                "R:R too low (%.2f < %.2f) — skipping trade", rr, self._cfg.min_risk_reward
            )
            return None

        capital_at_risk = self._cfg.account_capital * (self._cfg.risk_per_trade_pct / 100.0)
        qty = int(capital_at_risk / stop_distance)
        qty = max(1, min(qty, self._cfg.max_position_qty))

        direction = 1 if signal.action == Action.BUY else -1
        stop_price   = last_price - direction * stop_distance
        target_price = last_price + direction * target_distance

        self._log.info(
            "Sizing: capital_at_risk=%.2f stop_dist=%.2f qty=%d | "
            "stop=%.2f target=%.2f R:R=%.2f",
            capital_at_risk, stop_distance, qty, stop_price, target_price, rr,
        )
        return OrderIntent(
            action=signal.action,
            quantity=qty,
            stop_price=round(stop_price, 2),
            target_price=round(target_price, 2),
        )

    # ── Per-bar open trade monitoring ─────────────────────────────────────────

    def check_open_trade(self, symbol: str, current_high: float, current_low: float) -> Optional[OrderIntent]:
        """
        Called every bar while a trade is open. Returns an EXIT intent if
        the stop or target has been breached. Uses high/low of the bar, not
        just the close — a candle that spikes through the stop and recovers
        should still be treated as a stop-out.
        """
        if self._open_trade is None or self._open_trade.symbol != symbol:
            return None

        t = self._open_trade
        if t.direction == 1:   # long
            if current_low <= t.stop_price:
                self._log.warning(
                    "STOP HIT | low=%.2f <= stop=%.2f | symbol=%s",
                    current_low, t.stop_price, symbol,
                )
                return self._build_exit_intent(reason="stop")
            if current_high >= t.target_price:
                self._log.info(
                    "TARGET HIT | high=%.2f >= target=%.2f | symbol=%s",
                    current_high, t.target_price, symbol,
                )
                return self._build_exit_intent(reason="target")
        else:   # short (only active if allow_shorts=True)
            if current_high >= t.stop_price:
                self._log.warning("SHORT STOP HIT | symbol=%s", symbol)
                return self._build_exit_intent(reason="stop")
            if current_low <= t.target_price:
                self._log.info("SHORT TARGET HIT | symbol=%s", symbol)
                return self._build_exit_intent(reason="target")
        return None

    def _build_exit_intent(self, reason: str = "signal") -> Optional[OrderIntent]:
        if self._open_trade is None and self.current_position_qty == 0:
            return None
        qty = abs(self._open_trade.quantity if self._open_trade else self.current_position_qty)
        close_action = Action.SELL if self.current_position_qty > 0 else Action.BUY
        return OrderIntent(action=close_action, quantity=qty)

    # ── Fill recording ────────────────────────────────────────────────────────

    def record_fill(
        self,
        action: Action,
        quantity: int,
        entry_price: float,
        symbol: str = "",
        pnl_delta: float = 0.0,
        stop_price: float = 0.0,
        target_price: float = 0.0,
    ) -> None:
        signed_qty = quantity if action == Action.BUY else -quantity
        self.current_position_qty += signed_qty

        if action == Action.BUY and self._open_trade is None:
            # Opening a long
            self._open_trade = OpenTrade(
                symbol=symbol,
                entry_price=entry_price,
                quantity=quantity,
                direction=1,
                stop_price=stop_price,
                target_price=target_price,
            )
            self.trades_today += 1

        elif action == Action.SELL:
            if pnl_delta < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0
            self.realized_pnl_today += pnl_delta
            self._open_trade = None

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _effective_pnl(self, last_price: float) -> float:
        """Realized P&L + estimated unrealized from open trade."""
        if self._open_trade is None:
            return self.realized_pnl_today
        unrealized = (
            self._open_trade.direction
            * (last_price - self._open_trade.entry_price)
            * self._open_trade.quantity
        )
        return self.realized_pnl_today + unrealized

    def reset_daily(self) -> None:
        self.realized_pnl_today = 0.0
        self.trades_today = 0
        self.consecutive_losses = 0
        self._kill_switch = False
        # Do NOT reset _open_trade — if a trade carries overnight (shouldn't
        # happen in intraday, but defensively preserve it across the midnight
        # reset in case of unexpected failure to squareoff).
