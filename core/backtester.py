"""
core/backtester.py
Bar-by-bar backtest engine, updated to use the new RiskConfig /
RiskManager API with ATR-based sizing, stop/target enforcement,
and the updated record_fill() signature.

Fill timing: signal on bar i → filled at bar i+1 open.
Indicators: pass enriched_df built with fib_mode="rolling".
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import json
import tempfile

import numpy as np
import pandas as pd

from config.settings import RiskConfig
from core.strategy import Strategy, Action
from core.risk_manager import RiskManager
from core.portfolio import Portfolio


@dataclass
class BacktestCosts:
    slippage_bps: float = 5.0
    commission_per_order: float = 20.0


@dataclass
class BacktestConfig:
    initial_capital: float = 100_000.0
    costs: BacktestCosts = field(default_factory=BacktestCosts)
    state_window: int = 60
    annualization_factor: float = 252.0


@dataclass
class Trade:
    entry_time: datetime
    direction: str
    entry_price: float
    quantity: int
    stop_price: float = 0.0
    target_price: float = 0.0
    exit_time: datetime | None = None
    exit_price: float | None = None
    pnl: float | None = None
    exit_reason: str = ""


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list[Trade]
    metrics: dict

    def trades_to_dataframe(self) -> pd.DataFrame:
        if not self.trades:
            return pd.DataFrame()
        return pd.DataFrame([t.__dict__ for t in self.trades])

    def save(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        self.equity_curve.rename("equity").to_csv(
            output_dir / "equity_curve.csv", index_label="timestamp"
        )
        self.trades_to_dataframe().to_csv(output_dir / "trades.csv", index=False)
        with open(output_dir / "metrics.json", "w") as f:
            json.dump(self.metrics, f, indent=2, default=str)


def _default_risk_config(capital: float) -> RiskConfig:
    """Sensible backtest defaults when no external config is supplied."""
    return RiskConfig(
        account_capital=capital,
        max_daily_loss=capital * 0.02,
        max_daily_loss_pct=2.0,
        max_trades_per_day=10,
        risk_per_trade_pct=0.5,
        atr_stop_multiplier=1.5,
        atr_target_multiplier=2.5,
        min_risk_reward=1.5,
        max_position_qty=500,
        allow_shorts=False,
        max_consecutive_losses=5,
    )


class Backtester:
    def __init__(
        self,
        config: BacktestConfig,
        risk_config: RiskConfig | None = None,
        logger=None,
    ):
        self.cfg = config
        self._risk_cfg = risk_config or _default_risk_config(config.initial_capital)
        self._log = logger or _NullLogger()

    def run(
        self,
        enriched_df: pd.DataFrame,
        strategy: Strategy,
        symbol: str = "BACKTEST",
    ) -> BacktestResult:
        df = self._drop_warmup(enriched_df)
        n = len(df)
        if n < 5:
            raise ValueError(f"Only {n} usable bars after warm-up drop — need more history.")

        ledger_path = Path(tempfile.mkdtemp()) / "bt_ledger.csv"
        portfolio = Portfolio(self.cfg.initial_capital, ledger_path, self._log)
        risk = RiskManager(self._risk_cfg, self._log)

        equity_curve = pd.Series(index=df.index, dtype=float)
        trades: list[Trade] = []
        open_trade: Trade | None = None
        pending_intent = None
        last_date = None

        opens  = df["open"].to_numpy()
        highs  = df["high"].to_numpy()
        lows   = df["low"].to_numpy()
        closes = df["close"].to_numpy()
        atrs   = df["atr"].to_numpy() if "atr" in df.columns else np.zeros(n)

        for i in range(n):
            current_date = df.index[i].date()
            if last_date is not None and current_date != last_date:
                risk.reset_daily()
            last_date = current_date

            # ── Fill pending intent at this bar's open ─────────────────────
            if pending_intent is not None:
                fill_price = self._slip(opens[i], pending_intent.action)
                realized = portfolio.apply_fill(
                    symbol, pending_intent.action, pending_intent.quantity,
                    fill_price, commission=self.cfg.costs.commission_per_order,
                    order_id=f"BT-{i}", timestamp=df.index[i],
                )
                risk.record_fill(
                    pending_intent.action, pending_intent.quantity,
                    fill_price, symbol, realized,
                    stop_price=pending_intent.stop_price,
                    target_price=pending_intent.target_price,
                )
                open_trade = self._update_open_trade(
                    open_trade, trades,
                    pending_intent.action, pending_intent.quantity,
                    fill_price, df.index[i],
                    pending_intent.stop_price, pending_intent.target_price,
                )
                pending_intent = None

            # ── Mark to market ─────────────────────────────────────────────
            equity_curve.iloc[i] = portfolio.equity({symbol: closes[i]})

            # ── Check stop / target on open trade ──────────────────────────
            exit_intent = risk.check_open_trade(symbol, highs[i], lows[i])
            if exit_intent and i + 1 < n:
                # Exit filled at next bar's open (same fill-timing rule)
                pending_intent = exit_intent
                continue

            # ── Strategy signal ─────────────────────────────────────────────
            w_start = max(0, i - self.cfg.state_window + 1)
            signal  = strategy.generate_signal(df.iloc[w_start: i + 1])
            atr_val = float(atrs[i]) if not np.isnan(atrs[i]) else 0.0
            intent  = risk.evaluate(signal, closes[i], atr_val)
            if intent is not None and i + 1 < n:
                pending_intent = intent

        # Flush any still-open trade
        if open_trade is not None and open_trade.exit_time is None:
            trades.append(open_trade)

        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            metrics=self._metrics(equity_curve, trades),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _drop_warmup(df: pd.DataFrame) -> pd.DataFrame:
        indicator_cols = [
            c for c in df.columns
            if c not in {"open", "high", "low", "close", "volume"}
        ]
        if not indicator_cols:
            return df.copy()
        first_valid = df[indicator_cols].dropna(how="all").index.min()
        return df.loc[first_valid:].copy() if first_valid is not None else df.copy()

    def _slip(self, price: float, action: Action) -> float:
        bps = self.cfg.costs.slippage_bps / 10_000.0
        return price * (1 + bps) if action == Action.BUY else price * (1 - bps)

    @staticmethod
    def _update_open_trade(
        open_trade: Trade | None,
        trades: list[Trade],
        action: Action,
        qty: int,
        price: float,
        ts: datetime,
        stop: float = 0.0,
        target: float = 0.0,
    ) -> Trade | None:
        direction = "LONG" if action == Action.BUY else "SHORT"

        if open_trade is None:
            return Trade(
                entry_time=ts, direction=direction,
                entry_price=price, quantity=qty,
                stop_price=stop, target_price=target,
            )

        if open_trade.direction == direction:
            total = open_trade.quantity + qty
            open_trade.entry_price = (
                open_trade.entry_price * open_trade.quantity + price * qty
            ) / total
            open_trade.quantity = total
            return open_trade

        # Closing / flipping
        sign = 1 if open_trade.direction == "LONG" else -1
        realized = sign * (price - open_trade.entry_price) * min(open_trade.quantity, qty)
        open_trade.exit_time  = ts
        open_trade.exit_price = price
        open_trade.pnl = realized
        trades.append(open_trade)

        leftover = qty - open_trade.quantity
        if leftover > 0:
            return Trade(
                entry_time=ts, direction=direction,
                entry_price=price, quantity=leftover,
                stop_price=stop, target_price=target,
            )
        return None

    def _metrics(self, eq: pd.Series, trades: list[Trade]) -> dict:
        eq = eq.ffill().dropna()
        if len(eq) < 2:
            return {"error": "insufficient bars"}

        rets   = eq.pct_change().dropna()
        tot_r  = eq.iloc[-1] / eq.iloc[0] - 1
        years  = len(eq) / self.cfg.annualization_factor
        cagr   = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
        sharpe = (
            rets.mean() / rets.std() * np.sqrt(self.cfg.annualization_factor)
            if rets.std() > 0 else float("nan")
        )
        dd        = (eq - eq.cummax()) / eq.cummax()
        max_dd    = dd.min()
        closed    = [t for t in trades if t.pnl is not None]
        wins      = [t for t in closed if t.pnl > 0]
        losses    = [t for t in closed if t.pnl <= 0]
        g_profit  = sum(t.pnl for t in wins)
        g_loss    = abs(sum(t.pnl for t in losses))

        return {
            "total_return_pct":  round(tot_r  * 100, 2),
            "cagr_pct":          round(cagr   * 100, 2) if cagr == cagr else None,
            "sharpe_ratio":      round(sharpe,        3) if sharpe == sharpe else None,
            "max_drawdown_pct":  round(max_dd  * 100, 2),
            "num_trades":        len(closed),
            "win_rate_pct":      round(100 * len(wins) / len(closed), 2) if closed else None,
            "profit_factor":     round(g_profit / g_loss, 3) if g_loss > 0 else None,
            "avg_win":           round(g_profit / len(wins),    2) if wins   else None,
            "avg_loss":          round(-g_loss  / len(losses),  2) if losses else None,
            "final_equity":      round(float(eq.iloc[-1]),      2),
        }


class _NullLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass
