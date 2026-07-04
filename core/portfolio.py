"""
core/portfolio.py
Position and P&L ledger shared by paper trading and backtesting.

This is the piece that was missing before: main.py was calling
risk.record_fill(action, quantity) with no pnl_delta, which meant
RiskManager.realized_pnl_today never moved and the daily-loss kill
switch could never actually trip. Portfolio computes real realized
P&L using average-cost-basis accounting on every fill, and that
number is what now flows back into the risk manager.

Every fill is also appended to a CSV ledger on disk — that's your
audit trail and the raw data for backtest/paper-session reporting.
"""
from __future__ import annotations
import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.strategy import Action


@dataclass
class Position:
    symbol: str
    quantity: int = 0          # positive = long, negative = short
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        return self.quantity == 0


@dataclass
class LedgerEntry:
    timestamp: datetime
    symbol: str
    action: str
    quantity: int
    price: float
    commission: float
    realized_pnl: float
    position_after: int
    order_id: str


class Portfolio:
    """
    Average-cost-basis position tracker. One instance per running
    session (paper or live) — not per symbol; it holds a dict of
    positions internally so a single bot process can track multiple
    instruments if you extend main.py to do so later.
    """

    def __init__(self, initial_cash: float, ledger_path: Path, logger):
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self._positions: dict[str, Position] = {}
        self._ledger_path = ledger_path
        self._log = logger
        self._init_ledger_file()

    def _init_ledger_file(self) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._ledger_path.exists():
            with open(self._ledger_path, "w", newline="") as f:
                csv.writer(f).writerow(
                    ["timestamp", "symbol", "action", "quantity", "price",
                     "commission", "realized_pnl", "position_after", "order_id"]
                )

    def get_position(self, symbol: str) -> Position:
        return self._positions.setdefault(symbol, Position(symbol=symbol))

    def apply_fill(
        self,
        symbol: str,
        action: Action,
        quantity: int,
        price: float,
        commission: float = 0.0,
        order_id: str = "",
        timestamp: datetime | None = None,
    ) -> float:
        """
        Applies a fill, returns the realized P&L from this specific fill
        (0.0 if it only opened/added to a position rather than closing
        any of it). Average-cost-basis: realized P&L only crystallizes
        on the portion of a trade that reduces an existing position.
        """
        pos = self.get_position(symbol)
        signed_qty = quantity if action == Action.BUY else -quantity
        realized_pnl = 0.0

        same_direction = (pos.quantity >= 0 and signed_qty >= 0) or (pos.quantity <= 0 and signed_qty <= 0)

        if pos.is_flat or same_direction:
            # Opening or adding — blend into average cost, no P&L yet.
            new_qty = pos.quantity + signed_qty
            if new_qty != 0:
                pos.avg_price = (pos.avg_price * pos.quantity + price * signed_qty) / new_qty
            pos.quantity = new_qty
        else:
            # Reducing or flipping — the reducing portion realizes P&L
            # against the existing average cost.
            closing_qty = min(abs(signed_qty), abs(pos.quantity))
            direction = 1 if pos.quantity > 0 else -1
            realized_pnl = closing_qty * direction * (price - pos.avg_price)
            pos.quantity += signed_qty
            if pos.quantity == 0:
                pos.avg_price = 0.0
            elif (pos.quantity > 0) != (direction > 0):
                # Flipped through zero — remaining quantity is a fresh position.
                pos.avg_price = price

        # Cash flow already nets out the trade correctly without separately
        # adding realized_pnl — buying decreases cash by qty*price, selling
        # increases it by qty*price, and average-cost accounting is what
        # makes realized_pnl meaningful for reporting/risk, not for cash.
        self.cash += -signed_qty * price - commission

        self._append_ledger(timestamp or datetime.now(), symbol, action.value, quantity,
                             price, commission, realized_pnl, pos.quantity, order_id)
        self._log.info(
            "Fill: %s %d %s @ %.2f | realized_pnl=%.2f | position_after=%d | cash=%.2f",
            action.value, quantity, symbol, price, realized_pnl, pos.quantity, self.cash,
        )
        return realized_pnl

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """Unrealized P&L across all open positions at the given prices."""
        total = 0.0
        for symbol, pos in self._positions.items():
            if pos.is_flat or symbol not in prices:
                continue
            total += pos.quantity * (prices[symbol] - pos.avg_price)
        return total

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.mark_to_market(prices)

    def _append_ledger(self, timestamp, symbol, action, quantity, price,
                        commission, realized_pnl, position_after, order_id) -> None:
        with open(self._ledger_path, "a", newline="") as f:
            csv.writer(f).writerow(
                [timestamp.isoformat(), symbol, action, quantity, f"{price:.4f}",
                 f"{commission:.4f}", f"{realized_pnl:.4f}", position_after, order_id]
            )
