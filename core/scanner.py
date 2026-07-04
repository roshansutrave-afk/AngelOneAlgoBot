"""
core/scanner.py
Market scanner: runs the strategy against every instrument in a
given universe and returns a ranked list of signals.

Rate limiting is the core constraint here. AngelOne's historical
data API is not documented with hard rate limits, but empirical
observation puts the safe ceiling at ~1 req/sec sustained. Bursting
above that gets HTTP 429s that silently return empty data (the SDK
returns status=False rather than raising). This scanner enforces:
  - A per-request delay (default 1.1s — slightly over the 1 req/sec
    observed limit, giving headroom for network jitter).
  - A configurable ThreadPoolExecutor, but with max_workers=1 by
    default. Tempting to parallelize, but the rate limit applies to
    the API key, not per-thread — parallelism without a shared token
    bucket just causes 429s faster.
  - If you have a paid API plan with documented higher limits,
    increase requests_per_second in ScannerConfig accordingly.

Scanning 500 NSE equities at 1 req/sec = ~8.5 minutes wall time.
That's why UniverseFilter.max_nse_equity defaults to 500, not 2000:
a full F&O+equity sweep at 1/sec would take ~33 minutes, which is
fine for a nightly screen but not for an intraday loop.
"""
from __future__ import annotations
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd

from core.universe import Instrument
from core.data_feed import HistoricalDataFeed
from core.indicator_engine import IndicatorEngine, IndicatorConfig
from core.strategy import Strategy, Signal, Action


@dataclass
class ScannerConfig:
    interval: str = "ONE_DAY"
    lookback_days: int = 90
    requests_per_second: float = 0.9        # conservative — stays under API limit
    max_workers: int = 1                    # see module docstring on why 1 is right
    min_candles_required: int = 50          # skip illiquid symbols with sparse data
    only_actionable: bool = True            # only return BUY/SELL signals, drop HOLDs


@dataclass
class ScanResult:
    instrument: Instrument
    signal: Signal
    last_price: float
    last_bar_time: datetime
    enriched_df: pd.DataFrame | None = None  # only populated if keep_df=True in Scanner.run


class Scanner:
    def __init__(
        self,
        smart_connect_client,
        config: ScannerConfig,
        strategy: Strategy,
        logger,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ):
        self._client = smart_connect_client
        self._cfg = config
        self._strategy = strategy
        self._log = logger
        self._feed = HistoricalDataFeed(smart_connect_client, logger)
        self._engine = IndicatorEngine(IndicatorConfig(fib_mode="static"))
        self._progress_cb = progress_callback
        self._rate_lock = threading.Lock()
        self._last_request_time: float = 0.0

    def run(
        self,
        instruments: list[Instrument],
        keep_df: bool = False,
        stop_event: threading.Event | None = None,
    ) -> list[ScanResult]:
        results: list[ScanResult] = []
        total = len(instruments)
        completed = 0

        with ThreadPoolExecutor(max_workers=self._cfg.max_workers) as pool:
            futures = {
                pool.submit(self._scan_one, inst, keep_df): inst
                for inst in instruments
            }
            for future in as_completed(futures):
                if stop_event and stop_event.is_set():
                    self._log.info("Scanner: stop requested, cancelling remaining work")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break
                completed += 1
                inst = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        if not self._cfg.only_actionable or result.signal.action != Action.HOLD:
                            results.append(result)
                except Exception as exc:
                    self._log.debug("Scanner: %s/%s skipped: %s", inst.exchange, inst.symbol, exc)
                finally:
                    if self._progress_cb:
                        self._progress_cb(completed, total, inst.symbol)

        results.sort(key=lambda r: r.signal.confidence, reverse=True)
        self._log.info("Scan complete: %d/%d instruments produced actionable signals",
                       len(results), total)
        return results

    def _scan_one(self, inst: Instrument, keep_df: bool) -> ScanResult | None:
        self._rate_limit()
        to_date = datetime.now()
        from_date = to_date - timedelta(days=self._cfg.lookback_days)

        try:
            raw = self._feed.get_candles(
                inst.token, inst.candle_exchange,
                self._cfg.interval, from_date, to_date,
            )
        except Exception as exc:
            self._log.debug("Candle fetch failed for %s/%s: %s", inst.exchange, inst.symbol, exc)
            return None

        if len(raw) < self._cfg.min_candles_required:
            return None

        try:
            enriched = self._engine.transform(raw)
        except Exception:
            return None

        signal = self._strategy.generate_signal(enriched)
        last_price = float(enriched["close"].iloc[-1])
        last_bar_time = enriched.index[-1].to_pydatetime()

        return ScanResult(
            instrument=inst,
            signal=signal,
            last_price=last_price,
            last_bar_time=last_bar_time,
            enriched_df=enriched if keep_df else None,
        )

    def _rate_limit(self) -> None:
        """Token-bucket style throttle: wait until enough time has elapsed
        since the last API call to stay within requests_per_second."""
        min_gap = 1.0 / self._cfg.requests_per_second
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_time
            if elapsed < min_gap:
                time.sleep(min_gap - elapsed)
            self._last_request_time = time.monotonic()


def results_to_dataframe(results: list[ScanResult]) -> pd.DataFrame:
    """Flatten scan results into a tidy DataFrame for display or export."""
    if not results:
        return pd.DataFrame()
    rows = []
    for r in results:
        rows.append({
            "symbol": r.instrument.symbol,
            "name": r.instrument.name,
            "exchange": r.instrument.exchange,
            "instrument_type": r.instrument.instrument_type,
            "expiry": r.instrument.expiry,
            "action": r.signal.action.value,
            "confidence": round(r.signal.confidence, 3),
            "reason": r.signal.reason,
            "last_price": round(r.last_price, 2),
            "last_bar_time": r.last_bar_time,
            "lot_size": r.instrument.lot_size,
            "token": r.instrument.token,
        })
    return pd.DataFrame(rows).sort_values(["action", "confidence"], ascending=[True, False])
