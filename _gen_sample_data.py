"""One-off helper to generate a synthetic OHLCV CSV for smoke-testing backtest_runner.py
without needing live AngelOne credentials. Not part of the bot itself - safe to delete."""
import numpy as np
import pandas as pd

rng = pd.date_range("2022-01-01", periods=500, freq="D")
rs = np.random.default_rng(3)
close = 100 + np.cumsum(rs.normal(0.05, 1.2, len(rng)))
df = pd.DataFrame({
    "timestamp": rng,
    "open": close + rs.normal(0, 0.3, len(rng)),
    "high": close + np.abs(rs.normal(0.6, 0.3, len(rng))),
    "low": close - np.abs(rs.normal(0.6, 0.3, len(rng))),
    "close": close,
    "volume": rs.integers(1000, 10000, len(rng)),
})
df.to_csv("data/sample_sbin_daily.csv", index=False)
print(f"WROTE {len(df)} ROWS")
