"""
tests/test_backtester.py
Tests for the backtester, risk manager stop/target logic, and
the kill-switch P&L wiring. Updated to match the new RiskConfig
and RiskManager.evaluate(signal, price, atr) / record_fill() APIs.
"""
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from core.indicator_engine import IndicatorEngine, IndicatorConfig
from core.backtester import Backtester, BacktestConfig, _default_risk_config
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from config.settings import RiskConfig
from core.strategy import Strategy, Signal, Action


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


class _AlwaysBuy(Strategy):
    def generate_signal(self, df): return Signal(Action.BUY, 1.0, "test")


class _AlwaysHold(Strategy):
    def generate_signal(self, df): return Signal(Action.HOLD, 0.0, "hold")


def _make_ohlcv(n=200, seed=7) -> pd.DataFrame:
    rng = pd.date_range("2023-01-01", periods=n, freq="D")
    rs  = np.random.default_rng(seed)
    close = 100 + np.cumsum(rs.normal(0.05, 1.2, n))
    return pd.DataFrame({
        "open":   close + rs.normal(0, 0.3, n),
        "high":   close + np.abs(rs.normal(0.6, 0.3, n)),
        "low":    close - np.abs(rs.normal(0.6, 0.3, n)),
        "close":  close,
        "volume": rs.integers(5000, 50000, n),
    }, index=rng)


def _enriched(n=200, seed=7):
    cfg = IndicatorConfig(fib_mode="rolling", fib_lookback=20)
    return IndicatorEngine(cfg).transform(_make_ohlcv(n, seed))


# ── Backtester ─────────────────────────────────────────────────────────────────

def test_backtest_runs_produces_metrics():
    result = Backtester(BacktestConfig()).run(_enriched(), _AlwaysBuy())
    assert "sharpe_ratio" in result.metrics
    assert "final_equity" in result.metrics
    assert len(result.equity_curve.dropna()) > 0


def test_backtest_hold_strategy_no_trades():
    result = Backtester(BacktestConfig()).run(_enriched(), _AlwaysHold())
    closed = [t for t in result.trades if t.pnl is not None]
    assert len(closed) == 0


def test_open_trade_flushed_at_end():
    """A position still open when backtest ends must appear in trades list."""
    result = Backtester(BacktestConfig()).run(_enriched(), _AlwaysBuy())
    # AlwaysBuy will open a trade; without the flush it would vanish.
    assert len(result.trades) >= 1


# ── RiskManager stop/target ────────────────────────────────────────────────────

def _risk() -> RiskManager:
    cfg = RiskConfig(
        account_capital=100_000, max_daily_loss=2_000, max_daily_loss_pct=2.0,
        max_trades_per_day=10, risk_per_trade_pct=0.5,
        atr_stop_multiplier=1.5, atr_target_multiplier=2.5,
        min_risk_reward=1.5, max_position_qty=500,
        allow_shorts=False, max_consecutive_losses=5,
    )
    return RiskManager(cfg, _Log())


def test_atr_sizing_produces_sensible_qty():
    risk = _risk()
    signal = Signal(Action.BUY, 0.9, "test")
    # price=500, ATR=5 → stop_dist=7.5 → capital_at_risk=500 → qty≈66
    intent = risk.evaluate(signal, last_price=500.0, atr=5.0)
    assert intent is not None
    assert intent.quantity > 0
    assert intent.stop_price < 500.0
    assert intent.target_price > 500.0


def test_stop_hit_triggers_exit():
    risk = _risk()
    signal = Signal(Action.BUY, 0.9, "test")
    intent = risk.evaluate(signal, 500.0, 5.0)
    risk.record_fill(Action.BUY, intent.quantity, 500.0, "TEST",
                      0.0, intent.stop_price, intent.target_price)

    # Simulate price dropping below stop
    exit_intent = risk.check_open_trade("TEST", current_high=498.0, current_low=intent.stop_price - 1)
    assert exit_intent is not None
    assert exit_intent.action == Action.SELL


def test_target_hit_triggers_exit():
    risk = _risk()
    signal = Signal(Action.BUY, 0.9, "test")
    intent = risk.evaluate(signal, 500.0, 5.0)
    risk.record_fill(Action.BUY, intent.quantity, 500.0, "TEST",
                      0.0, intent.stop_price, intent.target_price)

    exit_intent = risk.check_open_trade("TEST", current_high=intent.target_price + 1, current_low=501.0)
    assert exit_intent is not None
    assert exit_intent.action == Action.SELL


def test_no_exit_when_price_in_range():
    risk = _risk()
    signal = Signal(Action.BUY, 0.9, "test")
    intent = risk.evaluate(signal, 500.0, 5.0)
    risk.record_fill(Action.BUY, intent.quantity, 500.0, "TEST",
                      0.0, intent.stop_price, intent.target_price)

    exit_intent = risk.check_open_trade("TEST", current_high=503.0, current_low=499.0)
    assert exit_intent is None


# ── Kill-switch wiring (the original bug) ─────────────────────────────────────

def test_kill_switch_fires_after_loss():
    cfg = RiskConfig(
        account_capital=100_000, max_daily_loss=50.0, max_daily_loss_pct=99.0,
        max_trades_per_day=100, risk_per_trade_pct=0.5,
        atr_stop_multiplier=1.5, atr_target_multiplier=2.5,
        min_risk_reward=1.5, max_position_qty=500,
        allow_shorts=False, max_consecutive_losses=999,
    )
    risk = RiskManager(cfg, _Log())
    portfolio = Portfolio(100_000, Path(tempfile.mkdtemp()) / "l.csv", _Log())

    # Buy 10 shares at 100, exit at 90 → realized loss = -100
    portfolio.apply_fill("T", Action.BUY,  10, 100.0)
    realized = portfolio.apply_fill("T", Action.SELL, 10, 90.0)
    assert realized == -100.0

    risk.record_fill(Action.SELL, 10, 90.0, "T", realized)
    assert risk.realized_pnl_today == -100.0

    # Next signal must be vetoed
    next_sig = Signal(Action.BUY, 1.0, "should be blocked")
    assert risk.evaluate(next_sig, 90.0, 5.0) is None


def test_consecutive_loss_pauses_entries():
    cfg = RiskConfig(
        account_capital=100_000, max_daily_loss=999_999, max_daily_loss_pct=99.0,
        max_trades_per_day=100, risk_per_trade_pct=0.5,
        atr_stop_multiplier=1.5, atr_target_multiplier=2.5,
        min_risk_reward=1.5, max_position_qty=500,
        allow_shorts=False, max_consecutive_losses=3,
    )
    risk = RiskManager(cfg, _Log())

    # Simulate 3 consecutive losses
    for _ in range(3):
        risk.record_fill(Action.BUY,  1, 100.0, "T", 0.0)
        risk.record_fill(Action.SELL, 1,  95.0, "T", -5.0)

    assert risk.consecutive_losses == 3
    sig = Signal(Action.BUY, 1.0, "blocked after streak")
    assert risk.evaluate(sig, 100.0, 5.0) is None


def test_allow_shorts_false_blocks_sell():
    risk = _risk()   # allow_shorts=False by default
    sig = Signal(Action.SELL, 0.9, "short attempt")
    assert risk.evaluate(sig, 500.0, 5.0) is None


def test_max_trades_per_day_cap():
    cfg = RiskConfig(
        account_capital=100_000, max_daily_loss=999_999, max_daily_loss_pct=99.0,
        max_trades_per_day=2, risk_per_trade_pct=0.5,
        atr_stop_multiplier=1.5, atr_target_multiplier=2.5,
        min_risk_reward=1.5, max_position_qty=500,
        allow_shorts=False, max_consecutive_losses=999,
    )
    risk = RiskManager(cfg, _Log())
    sig = Signal(Action.BUY, 0.9, "entry")

    for _ in range(2):
        intent = risk.evaluate(sig, 500.0, 5.0)
        assert intent is not None
        risk.record_fill(Action.BUY,  intent.quantity, 500.0, "T", 0.0,
                          intent.stop_price, intent.target_price)
        risk.record_fill(Action.SELL, intent.quantity, 505.0, "T", 5.0)

    # Third trade must be blocked
    assert risk.evaluate(sig, 500.0, 5.0) is None
