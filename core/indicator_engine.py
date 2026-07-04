"""
core/indicator_engine.py
Adds ATR and Volume MA on top of the existing SMA/BB/MACD/Fib set.
ATR is used by the risk manager for stop-loss placement and position
sizing. Volume MA is used by the strategy as a liquidity filter —
a MACD crossover on below-average volume is noise, not signal.
"""
from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd

REQUIRED_COLS = {"open", "high", "low", "close", "volume"}
FIB_RATIOS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


@dataclass
class IndicatorConfig:
    sma_period: int = 20
    bb_period: int = 20
    bb_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 14
    volume_ma_period: int = 20
    fib_lookback: int | None = None
    fib_mode: str = "static"  # "static" (live) or "rolling" (backtest)


class IndicatorEngine:
    def __init__(self, config: IndicatorConfig | None = None):
        self.cfg = config or IndicatorConfig()

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._validate(df)
        df = self._sma(df)
        df = self._bollinger(df)
        df = self._macd(df)
        df = self._atr(df)
        df = self._volume_ma(df)
        if self.cfg.fib_mode == "rolling":
            df = self._fibonacci_rolling(df)
        else:
            df = self._fibonacci(df)
        return df

    @staticmethod
    def _validate(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        missing = REQUIRED_COLS - set(df.columns)
        if missing:
            raise ValueError(f"Missing OHLCV columns: {missing}")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError("Index must be DatetimeIndex.")
        return df.sort_index()

    def _sma(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.cfg.sma_period
        df[f"sma_{p}"] = df["close"].rolling(p, min_periods=p).mean()
        # SMA slope: positive = uptrend strengthening
        df["sma_slope"] = df[f"sma_{p}"].diff(3)
        return df

    def _bollinger(self, df: pd.DataFrame) -> pd.DataFrame:
        p, k = self.cfg.bb_period, self.cfg.bb_std
        mid = df["close"].rolling(p, min_periods=p).mean()
        std = df["close"].rolling(p, min_periods=p).std(ddof=0)
        df["bb_mid"] = mid
        df["bb_upper"] = mid + k * std
        df["bb_lower"] = mid - k * std
        df["bb_percent_b"] = (df["close"] - df["bb_lower"]) / (df["bb_upper"] - df["bb_lower"])
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / mid
        return df

    def _macd(self, df: pd.DataFrame) -> pd.DataFrame:
        fast, slow, sig = self.cfg.macd_fast, self.cfg.macd_slow, self.cfg.macd_signal
        ema_fast = df["close"].ewm(span=fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=sig, adjust=False).mean()
        df["macd"] = macd_line
        df["macd_signal"] = signal_line
        df["macd_hist"] = macd_line - signal_line
        return df

    def _atr(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Wilder's ATR. Used for stop placement and position sizing.
        True Range = max(H-L, |H-Cprev|, |L-Cprev|)
        """
        p = self.cfg.atr_period
        high, low, close = df["high"], df["low"], df["close"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low  - prev_close).abs(),
        ], axis=1).max(axis=1)
        # Wilder smoothing = EWM with alpha = 1/period
        df["atr"] = tr.ewm(alpha=1.0 / p, adjust=False).mean()
        return df

    def _volume_ma(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.cfg.volume_ma_period
        df["volume_ma"] = df["volume"].rolling(p, min_periods=p).mean()
        # Relative volume: current bar volume vs its MA
        df["rel_volume"] = df["volume"] / df["volume_ma"]
        return df

    def _fibonacci(self, df: pd.DataFrame) -> pd.DataFrame:
        lb = self.cfg.fib_lookback
        window = df.iloc[-lb:] if lb else df
        swing_high_val = window["high"].max()
        swing_low_val  = window["low"].min()
        swing_high_idx = window["high"].idxmax()
        swing_low_idx  = window["low"].idxmin()
        diff = swing_high_val - swing_low_val
        uptrend = swing_low_idx < swing_high_idx
        for r in FIB_RATIOS:
            level = swing_high_val - diff * r if uptrend else swing_low_val + diff * r
            df[f"fib_{int(round(r * 1000)):04d}"] = level
        df.attrs["fib_swing_high"] = (swing_high_idx, swing_high_val)
        df.attrs["fib_swing_low"]  = (swing_low_idx,  swing_low_val)
        df.attrs["fib_trend"] = "up" if uptrend else "down"
        return df

    def _fibonacci_rolling(self, df: pd.DataFrame) -> pd.DataFrame:
        lb = self.cfg.fib_lookback
        if not lb or lb < 2:
            raise ValueError("fib_mode='rolling' requires fib_lookback >= 2")
        high = df["high"].to_numpy()
        low  = df["low"].to_numpy()
        n    = len(df)
        cols = {f"fib_{int(round(r * 1000)):04d}": np.full(n, np.nan) for r in FIB_RATIOS}
        trend = np.full(n, "", dtype=object)
        for i in range(lb - 1, n):
            h_win = high[i - lb + 1: i + 1]
            l_win = low[i  - lb + 1: i + 1]
            h_rel, l_rel = int(np.argmax(h_win)), int(np.argmin(l_win))
            swing_high, swing_low = h_win[h_rel], l_win[l_rel]
            diff = swing_high - swing_low
            uptrend = l_rel < h_rel
            for r in FIB_RATIOS:
                level = swing_high - diff * r if uptrend else swing_low + diff * r
                cols[f"fib_{int(round(r * 1000)):04d}"][i] = level
            trend[i] = "up" if uptrend else "down"
        for col, arr in cols.items():
            df[col] = arr
        df["fib_trend_rolling"] = trend
        return df
