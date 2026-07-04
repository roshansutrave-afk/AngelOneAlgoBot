"""
tests/test_universe_scanner.py
Tests that don't require a live AngelOne connection — the universe
builder and scanner are tested against a fake scrip master injected
via monkeypatching, so these run offline.
"""
import sys
import threading
import tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest

from core.universe import UniverseBuilder, UniverseFilter, Instrument
from core.scanner import Scanner, ScannerConfig, results_to_dataframe
from core.strategy import Strategy, Signal, Action
from core.indicator_engine import IndicatorEngine, IndicatorConfig


class _DummyLogger:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def error(self, *a, **k): pass
    def debug(self, *a, **k): pass
    def exception(self, *a, **k): pass


# Fake scrip master using the actual column set from the live AngelOne
# scrip master (no 'series' column — series is embedded in the symbol
# suffix, e.g. "SBIN-EQ", "SBIN-BE").
FAKE_MASTER = pd.DataFrame([
    {"token": "3045", "symbol": "SBIN-EQ", "name": "STATE BANK OF INDIA",
     "exch_seg": "NSE", "instrumenttype": "EQ", "lotsize": "1",
     "tick_size": "0.05", "expiry": "", "strike": "-1.0", "freeze_qty": "1"},
    {"token": "1594", "symbol": "INFY-EQ", "name": "INFOSYS LIMITED",
     "exch_seg": "NSE", "instrumenttype": "EQ", "lotsize": "1",
     "tick_size": "0.05", "expiry": "", "strike": "-1.0", "freeze_qty": "1"},
    {"token": "9999", "symbol": "SBIN-BE", "name": "SBIN BE SERIES",
     "exch_seg": "NSE", "instrumenttype": "EQ", "lotsize": "1",
     "tick_size": "0.05", "expiry": "", "strike": "-1.0", "freeze_qty": "1"},
    {"token": "7001", "symbol": "SBIN25JUNFUT", "name": "SBIN FUTURES",
     "exch_seg": "NFO", "instrumenttype": "FUTSTK", "lotsize": "1500",
     "tick_size": "0.05", "expiry": "25JUN2025", "strike": "-1.0", "freeze_qty": "1"},
    {"token": "8001", "symbol": "GOLDM25JUNFUT", "name": "GOLD MINI FUTURES",
     "exch_seg": "MCX", "instrumenttype": "FUTCOM", "lotsize": "100",
     "tick_size": "1.0", "expiry": "25JUN2025", "strike": "-1.0", "freeze_qty": "1"},
])


def _make_builder(monkeypatch) -> UniverseBuilder:
    cache = Path(tempfile.mkdtemp())
    builder = UniverseBuilder(cache, _DummyLogger())
    # Inject the fake master so no HTTP call is made
    builder._master._df = FAKE_MASTER
    return builder


def test_nse_equity_filters_non_eq_series(monkeypatch):
    builder = _make_builder(monkeypatch)
    filt = UniverseFilter(include_nse_equity=True, include_bse_equity=False,
                          include_nfo_futures=False, max_nse_equity=None)
    universe = builder.build(filt)
    symbols = {i.symbol for i in universe}
    assert "SBIN-EQ" in symbols
    assert "INFY-EQ" in symbols
    assert "SBIN-BE" not in symbols   # BE series must be excluded


def test_nfo_futures_excluded_options(monkeypatch):
    builder = _make_builder(monkeypatch)
    fake_with_option = pd.concat([FAKE_MASTER, pd.DataFrame([{
        "token": "7002", "symbol": "SBINCE2590PE", "name": "SBIN OPTION",
        "exch_seg": "NFO", "instrumenttype": "OPTSTK", "lotsize": "1500",
        "tick_size": "0.05", "expiry": "25JUN2025", "strike": "900.0",
        "freeze_qty": "1",
    }])], ignore_index=True)
    builder._master._df = fake_with_option
    filt = UniverseFilter(include_nse_equity=False, include_nfo_futures=True,
                          max_nfo_futures=None)
    universe = builder.build(filt)
    types = {i.instrument_type for i in universe}
    assert "OPTSTK" not in types
    assert "FUTSTK" in types


def test_max_cap_respected(monkeypatch):
    builder = _make_builder(monkeypatch)
    filt = UniverseFilter(include_nse_equity=True, include_bse_equity=False,
                          include_nfo_futures=False, max_nse_equity=1)
    universe = builder.build(filt)
    assert len(universe) == 1


def test_scanner_with_mock_client():
    """Scanner returns BUY signals from a strategy that always returns BUY,
    and never errors on well-formed instruments even with a mocked data feed."""

    # Build a minimal 80-bar OHLCV dataframe
    def _fake_candles(token, exchange, interval, from_date, to_date):
        rng = pd.date_range("2024-01-01", periods=80, freq="D")
        rs = np.random.default_rng(int(token))
        close = 100 + np.cumsum(rs.normal(0.05, 1.0, 80))
        return pd.DataFrame({
            "open": close + rs.normal(0, 0.2, 80),
            "high": close + np.abs(rs.normal(0.4, 0.2, 80)),
            "low": close - np.abs(rs.normal(0.4, 0.2, 80)),
            "close": close, "volume": rs.integers(1000, 5000, 80),
        }, index=rng)

    class _MockFeed:
        def get_candles(self, token, exchange, interval, from_date, to_date):
            return _fake_candles(token, exchange, interval, from_date, to_date)

    class _AlwaysBuy(Strategy):
        def generate_signal(self, df):
            return Signal(Action.BUY, 0.9, "always buy")

    instruments = [
        Instrument(token="3045", symbol="SBIN-EQ", name="SBI", exchange="NSE",
                   candle_exchange="NSE", instrument_type="EQ", lot_size=1,
                   tick_size=0.05, expiry=""),
        Instrument(token="1594", symbol="INFY-EQ", name="INFY", exchange="NSE",
                   candle_exchange="NSE", instrument_type="EQ", lot_size=1,
                   tick_size=0.05, expiry=""),
    ]

    # Inject mock feed into scanner
    scanner = Scanner(None, ScannerConfig(only_actionable=True), _AlwaysBuy(), _DummyLogger())
    scanner._feed = _MockFeed()

    results = scanner.run(instruments)
    assert len(results) == 2
    assert all(r.signal.action == Action.BUY for r in results)
    assert all(r.signal.confidence == 0.9 for r in results)


def test_results_to_dataframe_empty():
    df = results_to_dataframe([])
    assert df.empty


def test_stop_event_cancels_scan():
    """A pre-set stop event should cause the scan to return before all
    instruments are processed (or at least not crash)."""

    class _SlowFeed:
        def get_candles(self, *a, **k):
            import time; time.sleep(0.05)
            raise RuntimeError("deliberately fails")

    instruments = [
        Instrument(token=str(i), symbol=f"SYM{i}", name=f"SYM{i}", exchange="NSE",
                   candle_exchange="NSE", instrument_type="EQ",
                   lot_size=1, tick_size=0.05, expiry="")
        for i in range(20)
    ]

    scanner = Scanner(None, ScannerConfig(), TrendMomentumStrategyStub(), _DummyLogger())
    scanner._feed = _SlowFeed()

    stop = threading.Event()
    stop.set()  # pre-set: cancel immediately

    results = scanner.run(instruments, stop_event=stop)
    assert isinstance(results, list)  # must not raise


class TrendMomentumStrategyStub(Strategy):
    def generate_signal(self, df):
        return Signal(Action.HOLD, 0.0, "stub")
