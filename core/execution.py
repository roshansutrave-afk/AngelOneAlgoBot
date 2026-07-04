"""
core/execution.py
Order execution boundary. Handles PAPER and LIVE modes.
Updated to pass stop/target prices into RiskManager.record_fill()
so the open-trade stop/target enforcement has correct prices.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config.settings import ExecutionCosts
from core.strategy import Action
from core.risk_manager import OrderIntent
from core.portfolio import Portfolio
from core.charges import ProductType, calculate_charges
from core.excel_logger import ExcelLogger


@dataclass
class Fill:
    action: Action
    quantity: int
    price: float
    timestamp: datetime
    order_id: str
    realized_pnl: float
    total_charges: float
    stop_price: float = 0.0
    target_price: float = 0.0


class ExecutionEngine:
    def __init__(
        self,
        smart_connect_client,
        trading_mode: str,
        exchange: str,
        costs: ExecutionCosts,
        portfolio: Portfolio,
        logger,
        excel_logger: Optional[ExcelLogger] = None,
        product_type: ProductType = ProductType.INTRADAY,
        strategy_name: str = "TrendMomentum",
    ):
        self._client = smart_connect_client
        self._mode = trading_mode
        self._exchange = exchange
        self._costs = costs
        self._portfolio = portfolio
        self._log = logger
        self._excel = excel_logger
        self._product_type = product_type
        self._strategy_name = strategy_name

    def execute(
        self,
        intent: OrderIntent,
        symbol: str,
        symbol_token: str,
        last_price: float,
        notes: str = "",
    ) -> Fill:
        ts = datetime.now()

        if self._mode == "PAPER":
            fill_price = self._apply_slippage(last_price, intent.action)
            order_id = f"PAPER-{ts.strftime('%Y%m%d%H%M%S')}"

            leg_costs = calculate_charges(
                fill_price, intent.quantity, self._product_type,
                intent.action.value, self._costs.commission_per_order,
            )
            realized = self._portfolio.apply_fill(
                symbol, intent.action, intent.quantity, fill_price,
                commission=leg_costs.total, order_id=order_id, timestamp=ts,
            )

            if self._excel:
                if intent.action == Action.BUY:
                    self._excel.log_entry(
                        symbol=symbol, exchange=self._exchange,
                        strategy=self._strategy_name, side="BUY",
                        qty=intent.quantity, price=fill_price,
                        timestamp=ts, notes=notes,
                    )
                else:
                    self._excel.log_exit(
                        symbol=symbol, exit_price=fill_price,
                        timestamp=ts, notes=notes,
                    )

            self._log.info(
                "[PAPER] %s %d x %s @ %.2f | stop=%.2f target=%.2f | "
                "charges=%.2f (STT=%.2f broker=%.2f other=%.2f)",
                intent.action.value, intent.quantity, symbol, fill_price,
                intent.stop_price, intent.target_price,
                leg_costs.total, leg_costs.stt, leg_costs.brokerage, leg_costs.other,
            )
            return Fill(
                intent.action, intent.quantity, fill_price, ts,
                order_id, realized, leg_costs.total,
                intent.stop_price, intent.target_price,
            )

        # ── LIVE ─────────────────────────────────────────────────────────────
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": symbol,
            "symboltoken": symbol_token,
            "transactiontype": intent.action.value,
            "exchange": self._exchange,
            "ordertype": "MARKET",
            "producttype": "INTRADAY",
            "duration": "DAY",
            "price": "0",
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(intent.quantity),
        }
        resp = self._client.placeOrder(order_params)
        self._log.info("[LIVE] placeOrder response: %s", resp)
        order_id = (
            resp if isinstance(resp, str)
            else resp.get("data", {}).get("orderid", "UNKNOWN")
        )

        leg_costs = calculate_charges(
            last_price, intent.quantity, self._product_type,
            intent.action.value, self._costs.commission_per_order,
        )
        realized = self._portfolio.apply_fill(
            symbol, intent.action, intent.quantity, last_price,
            commission=leg_costs.total, order_id=str(order_id), timestamp=ts,
        )

        if self._excel:
            if intent.action == Action.BUY:
                self._excel.log_entry(symbol=symbol, exchange=self._exchange,
                                       strategy=self._strategy_name, side="BUY",
                                       qty=intent.quantity, price=last_price,
                                       timestamp=ts, notes=notes)
            else:
                self._excel.log_exit(symbol=symbol, exit_price=last_price,
                                      timestamp=ts, notes=notes)

        return Fill(
            intent.action, intent.quantity, last_price, ts,
            str(order_id), realized, leg_costs.total,
            intent.stop_price, intent.target_price,
        )

    def force_squareoff(
        self, symbol: str, symbol_token: str, last_price: float
    ) -> Optional[Fill]:
        pos = self._portfolio.get_position(symbol)
        if pos.is_flat:
            return None
        close_action = Action.SELL if pos.quantity > 0 else Action.BUY
        intent = OrderIntent(action=close_action, quantity=abs(pos.quantity))
        self._log.warning(
            "[SQUAREOFF] Forcing close: %d x %s @ ~%.2f",
            pos.quantity, symbol, last_price,
        )
        return self.execute(intent, symbol, symbol_token, last_price, notes="EOD squareoff")

    def _apply_slippage(self, price: float, action: Action) -> float:
        bps = self._costs.slippage_bps / 10_000.0
        return price * (1 + bps) if action == Action.BUY else price * (1 - bps)
