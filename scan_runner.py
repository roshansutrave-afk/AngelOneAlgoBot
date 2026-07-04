"""
scan_runner.py
CLI entry point for running a full market scan.

Usage:
  python scan_runner.py                         # NSE equity + NFO futures
  python scan_runner.py --segments NSE          # NSE equity only
  python scan_runner.py --segments NSE NFO MCX  # equities + all futures
  python scan_runner.py --max-nse 100 --interval ONE_HOUR
  python scan_runner.py --output scan_results/my_scan.csv

The scan runs sequentially at ~1 req/sec by design (see scanner.py).
A 500-symbol NSE scan takes ~9 minutes. Plan accordingly.
"""
from __future__ import annotations
import argparse
import sys
from datetime import datetime
from pathlib import Path

from config.settings import load_settings
from core.logger import get_logger
from core.auth import AngelOneSession
from core.universe import UniverseBuilder, UniverseFilter
from core.scanner import Scanner, ScannerConfig, results_to_dataframe
from strategies.trend_momentum import TrendMomentumStrategy

BASE_DIR = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan AngelOne universe for trading signals.")
    parser.add_argument("--segments", nargs="+",
                        choices=["NSE", "BSE", "NFO", "MCX", "CDS"],
                        default=["NSE", "NFO"],
                        help="Exchange segments to scan (default: NSE NFO)")
    parser.add_argument("--interval", default="ONE_DAY",
                        choices=["ONE_MINUTE", "FIVE_MINUTE", "FIFTEEN_MINUTE",
                                 "THIRTY_MINUTE", "ONE_HOUR", "ONE_DAY"],
                        help="Candle interval (default: ONE_DAY)")
    parser.add_argument("--lookback-days", type=int, default=90,
                        help="Days of historical data to fetch per symbol (default: 90)")
    parser.add_argument("--max-nse", type=int, default=500,
                        help="Max NSE equity symbols to scan (default: 500)")
    parser.add_argument("--max-nfo", type=int, default=200,
                        help="Max NFO futures symbols to scan (default: 200)")
    parser.add_argument("--max-mcx", type=int, default=50)
    parser.add_argument("--max-cds", type=int, default=30)
    parser.add_argument("--all-signals", action="store_true",
                        help="Include HOLD signals (default: only BUY/SELL)")
    parser.add_argument("--output", type=str, default="",
                        help="Path to save CSV of results (default: scan_results/YYYYMMDD_HHMM.csv)")
    args = parser.parse_args()

    settings = load_settings()
    logger = get_logger("scanner", BASE_DIR / "logs", settings.log_level)

    logger.info("Logging in to AngelOne...")
    session = AngelOneSession(settings.creds, logger)
    session.login()

    filt = UniverseFilter(
        include_nse_equity="NSE" in args.segments,
        include_bse_equity="BSE" in args.segments,
        include_nfo_futures="NFO" in args.segments,
        include_mcx_futures="MCX" in args.segments,
        include_cds_futures="CDS" in args.segments,
        max_nse_equity=args.max_nse,
        max_nfo_futures=args.max_nfo,
        max_mcx_futures=args.max_mcx,
        max_cds_futures=args.max_cds,
    )

    logger.info("Building instrument universe...")
    builder = UniverseBuilder(BASE_DIR / "data" / "instruments", logger)
    universe = builder.build(filt)

    if not universe:
        logger.error("No instruments matched the filter — check scrip master / segment selection")
        sys.exit(1)

    total = len(universe)
    logger.info("Universe ready: %d instruments. Estimated scan time: ~%d minutes at 1 req/sec",
                total, total // 60 + 1)

    def on_progress(done: int, out_of: int, symbol: str) -> None:
        pct = 100 * done / out_of
        bar = "#" * (done * 30 // out_of) + "-" * (30 - done * 30 // out_of)
        print(f"\r  [{bar}] {pct:5.1f}%  {done}/{out_of}  {symbol:<20}", end="", flush=True)

    scanner_cfg = ScannerConfig(
        interval=args.interval,
        lookback_days=args.lookback_days,
        only_actionable=not args.all_signals,
    )
    strategy = TrendMomentumStrategy()
    scanner = Scanner(session.client, scanner_cfg, strategy, logger, progress_callback=on_progress)

    print(f"\nScanning {total} instruments on {args.segments}...")
    results = scanner.run(universe)
    print()  # newline after progress bar

    df = results_to_dataframe(results)

    if df.empty:
        print("No actionable signals found in this scan.")
        return

    # Determine output path
    if args.output:
        out_path = Path(args.output)
    else:
        out_dir = BASE_DIR / "scan_results"
        out_dir.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = out_dir / f"scan_{stamp}.csv"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)

    # Print ranked summary to terminal
    print(f"\n{'='*70}")
    print(f"  SCAN RESULTS  —  {len(df)} actionable signals from {total} instruments")
    print(f"{'='*70}")
    buy_df = df[df["action"] == "BUY"].head(20)
    sell_df = df[df["action"] == "SELL"].head(20)

    if not buy_df.empty:
        print(f"\n  TOP BUY SIGNALS ({len(buy_df)} shown):")
        print(f"  {'SYMBOL':<18} {'EXCHANGE':<8} {'CONFIDENCE':>10}  REASON")
        for _, row in buy_df.iterrows():
            print(f"  {row['symbol']:<18} {row['exchange']:<8} {row['confidence']:>10.3f}  {row['reason']}")

    if not sell_df.empty:
        print(f"\n  TOP SELL SIGNALS ({len(sell_df)} shown):")
        print(f"  {'SYMBOL':<18} {'EXCHANGE':<8} {'CONFIDENCE':>10}  REASON")
        for _, row in sell_df.iterrows():
            print(f"  {row['symbol']:<18} {row['exchange']:<8} {row['confidence']:>10.3f}  {row['reason']}")

    print(f"\n  Full results saved to: {out_path.resolve()}")


if __name__ == "__main__":
    main()
