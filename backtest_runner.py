"""
backtest_runner.py
CLI entry point for running a backtest, either against:
  - A local CSV (timestamp,open,high,low,close,volume) via --csv — no
    API credentials needed, fast to iterate on.
  - Live historical data fetched from AngelOne via --symbol/--exchange/
    --interval/--days (requires .env credentials).

Always uses IndicatorEngine(fib_mode="rolling") — backtests never use
the "static" fib mode, which would leak future swing points into past
signals.

Usage:
  python backtest_runner.py --csv data\\historical\\sbin_daily.csv
  python backtest_runner.py --symbol SBIN-EQ --exchange NSE --interval ONE_DAY --days 365
"""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config.settings import RiskLimits
from core.logger import get_logger
from core.indicator_engine import IndicatorEngine, IndicatorConfig
from core.backtester import Backtester, BacktestConfig, BacktestCosts
from strategies.trend_momentum import TrendMomentumStrategy

BASE_DIR = Path(__file__).resolve().parent


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
    return df.sort_index()


def fetch_from_api(symbol: str, exchange: str, interval: str, days: int, logger) -> pd.DataFrame:
    from config.settings import load_settings
    from core.auth import AngelOneSession
    from core.instruments import InstrumentMaster
    from core.data_feed import HistoricalDataFeed

    settings = load_settings()
    session = AngelOneSession(settings.creds, logger)
    session.login()
    instruments = InstrumentMaster(settings.base_dir / "data" / "instruments", logger)
    token = instruments.lookup_token(symbol, exchange)
    feed = HistoricalDataFeed(session.client, logger)
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    return feed.get_candles(token, exchange, interval, from_date, to_date)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a backtest against the trend/momentum strategy.")
    parser.add_argument("--csv", type=str, help="Path to a local OHLCV CSV (timestamp,open,high,low,close,volume)")
    parser.add_argument("--symbol", type=str, default="SBIN-EQ")
    parser.add_argument("--exchange", type=str, default="NSE")
    parser.add_argument("--interval", type=str, default="ONE_DAY")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--initial-capital", type=float, default=100_000.0)
    parser.add_argument("--fib-lookback", type=int, default=60)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--commission", type=float, default=20.0)
    parser.add_argument("--max-position-qty", type=int, default=1)
    parser.add_argument("--output-dir", type=str, default="backtest_results")
    args = parser.parse_args()

    logger = get_logger("backtester", BASE_DIR / "logs", "INFO")

    if args.csv:
        logger.info("Loading historical data from CSV: %s", args.csv)
        raw = load_csv(Path(args.csv))
    else:
        logger.info("Fetching %d days of %s for %s:%s from AngelOne", args.days, args.interval, args.exchange, args.symbol)
        raw = fetch_from_api(args.symbol, args.exchange, args.interval, args.days, logger)

    engine = IndicatorEngine(IndicatorConfig(fib_mode="rolling", fib_lookback=args.fib_lookback))
    enriched = engine.transform(raw)

    risk_limits = RiskLimits(
        max_position_qty=args.max_position_qty,
        max_daily_loss=args.initial_capital * 0.05,
        risk_per_trade_pct=1.0,
    )
    bt_config = BacktestConfig(
        initial_capital=args.initial_capital,
        costs=BacktestCosts(slippage_bps=args.slippage_bps, commission_per_order=args.commission),
    )
    backtester = Backtester(bt_config, risk_limits, logger)
    strategy = TrendMomentumStrategy()

    result = backtester.run(enriched, strategy, symbol=args.symbol)

    print("\n=== BACKTEST RESULTS ===")
    for k, v in result.metrics.items():
        print(f"{k:>20}: {v}")

    output_dir = BASE_DIR / args.output_dir
    result.save(output_dir)

    fig, ax = plt.subplots(figsize=(10, 5))
    result.equity_curve.plot(ax=ax, title=f"Equity Curve - {args.symbol}")
    ax.set_ylabel("Equity")
    fig.tight_layout()
    fig.savefig(output_dir / "equity_curve.png", dpi=120)
    print(f"\nSaved trades.csv, equity_curve.csv, metrics.json, equity_curve.png to {output_dir.resolve()}")


if __name__ == "__main__":
    main()
