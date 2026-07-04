"""
tests/test_indicator_engine.py
Tests for ATR and volume MA additions alongside the original indicators.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
import pytest
from core.indicator_engine import IndicatorEngine, IndicatorConfig


def _make_ohlcv(n=150, seed=1) -> pd.DataFrame:
    rng = pd.date_range("2024-01-01", periods=n, freq="D")
    rs  = np.random.default_rng(seed)
    close = 100 + np.cumsum(rs.normal(0, 1, n))
    return pd.DataFrame({
        "open":   close + rs.normal(0, 0.3, n),
        "high":   close + np.abs(rs.normal(0.5, 0.3, n)),
        "low":    close - np.abs(rs.normal(0.5, 0.3, n)),
        "close":  close,
        "volume": rs.integers(1000, 10000, n),
    }, index=rng)


def test_all_indicator_columns_present():
    df = IndicatorEngine(IndicatorConfig()).transform(_make_ohlcv())
    required = ["sma_20", "sma_slope", "bb_upper", "bb_lower",
                "bb_percent_b", "macd", "macd_hist", "atr",
                "volume_ma", "rel_volume", "fib_0500"]
    for col in required:
        assert col in df.columns, f"Missing column: {col}"


def test_atr_always_positive():
    df = IndicatorEngine().transform(_make_ohlcv())
    atr = df["atr"].dropna()
    assert (atr > 0).all(), "ATR must be strictly positive"


def test_rel_volume_ratio_correct():
    df = IndicatorEngine().transform(_make_ohlcv())
    valid = df.dropna(subset=["rel_volume", "volume_ma"])
    for _, row in valid.iterrows():
        expected = row["volume"] / row["volume_ma"]
        assert abs(row["rel_volume"] - expected) < 1e-6


def test_sma_slope_is_diff_of_sma():
    df = IndicatorEngine().transform(_make_ohlcv())
    # slope = sma.diff(3) — check a spot
    valid = df.dropna(subset=["sma_slope", "sma_20"])
    i = 30
    manual = valid["sma_20"].iloc[i] - valid["sma_20"].iloc[i - 3]
    assert abs(valid["sma_slope"].iloc[i] - manual) < 1e-8


def test_rolling_fib_causality():
    df = _make_ohlcv(150)
    cfg = IndicatorConfig(fib_mode="rolling", fib_lookback=20)
    full      = IndicatorEngine(cfg).transform(df.copy())
    truncated = IndicatorEngine(cfg).transform(df.iloc[:100].copy())
    fib_cols  = [c for c in full.columns if c.startswith("fib_") and c != "fib_trend_rolling"]
    pd.testing.assert_series_equal(
        full.iloc[50][fib_cols],
        truncated.iloc[50][fib_cols],
        check_names=False,
    )


def test_raises_on_missing_columns():
    bad = pd.DataFrame({"close": [1, 2, 3]},
                       index=pd.date_range("2024-01-01", periods=3))
    with pytest.raises(ValueError):
        IndicatorEngine().transform(bad)
