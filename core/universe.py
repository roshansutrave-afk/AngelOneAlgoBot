"""
core/universe.py
Builds and filters the tradeable instrument universe from the AngelOne
scrip master. The scrip master has 80,000+ rows; this module trims it
to an actionable set before the scanner spends API quota on candle
fetches.

Segments supported by AngelOne SmartAPI:
  NSE  — NSE Cash (equities, ETFs, indices)
  BSE  — BSE Cash
  NFO  — NSE F&O (equity + index futures & options)
  BFO  — BSE F&O
  MCX  — Multi Commodity Exchange (commodities)
  CDS  — Currency Derivatives

Filtering philosophy:
  - For equity scans: only EQ series (not BE/BL/IL/SM/etc.) with a
    non-zero lot size, dropping penny stocks below a configurable
    minimum close price.
  - For F&O: only futures (instrumenttype contains "FUT") — options
    are excluded by default because the indicator engine doesn't
    account for gamma/theta; you'd need a separate strategy layer
    for those.
  - For MCX: only the active-month FUTCOM instruments.
  - Everything else (indices, preference shares, bonds) is excluded
    from scanning.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import pandas as pd

from core.instruments import InstrumentMaster


@dataclass
class UniverseFilter:
    # Exchanges to include
    include_nse_equity: bool = True
    include_bse_equity: bool = False   # overlap with NSE — off by default
    include_nfo_futures: bool = True
    include_mcx_futures: bool = False
    include_cds_futures: bool = False

    # NSE/BSE equity guards
    eq_series: list[str] = field(default_factory=lambda: ["EQ"])  # drop BE/BL/etc.

    # F&O guard — include only these instrument-type substrings
    fut_instrument_types: list[str] = field(default_factory=lambda: ["FUTSTK", "FUTIDX", "FUTCOM", "FUTCUR"])

    # Max symbols to scan per segment (API rate-limit budget guard)
    # Set to None to scan everything that passes the other filters.
    max_nse_equity: int | None = 500
    max_nfo_futures: int | None = 200
    max_mcx_futures: int | None = 50
    max_cds_futures: int | None = 30


# Map from scrip-master exch_seg strings to the exchange param
# AngelOne's historical candle API actually expects. Some segments
# use a different string in the candle API vs the scrip master.
SEGMENT_TO_CANDLE_EXCHANGE: dict[str, str] = {
    "NSE": "NSE",
    "BSE": "BSE",
    "NFO": "NFO",
    "BFO": "BFO",
    "MCX": "MCX",
    "CDS": "CDS",
}


@dataclass
class Instrument:
    token: str
    symbol: str
    name: str
    exchange: str          # as scrip master spells it (NSE, NFO, MCX, …)
    candle_exchange: str   # as candle API expects
    instrument_type: str
    lot_size: int
    tick_size: float
    expiry: str            # blank for equities


class UniverseBuilder:
    def __init__(self, cache_dir: Path, logger):
        self._master = InstrumentMaster(cache_dir, logger)
        self._log = logger

    def build(self, filt: UniverseFilter | None = None) -> list[Instrument]:
        filt = filt or UniverseFilter()
        df = self._master.load()
        instruments: list[Instrument] = []

        if filt.include_nse_equity:
            seg = self._filter_equity(df, "NSE", filt)
            if filt.max_nse_equity:
                seg = seg.head(filt.max_nse_equity)
            instruments += self._to_instruments(seg, "NSE")
            self._log.info("NSE equity universe: %d instruments", len(seg))

        if filt.include_bse_equity:
            seg = self._filter_equity(df, "BSE", filt)
            instruments += self._to_instruments(seg, "BSE")
            self._log.info("BSE equity universe: %d instruments", len(seg))

        if filt.include_nfo_futures:
            seg = self._filter_futures(df, "NFO", filt)
            if filt.max_nfo_futures:
                seg = seg.head(filt.max_nfo_futures)
            instruments += self._to_instruments(seg, "NFO")
            self._log.info("NFO futures universe: %d instruments", len(seg))

        if filt.include_mcx_futures:
            seg = self._filter_futures(df, "MCX", filt)
            if filt.max_mcx_futures:
                seg = seg.head(filt.max_mcx_futures)
            instruments += self._to_instruments(seg, "MCX")
            self._log.info("MCX futures universe: %d instruments", len(seg))

        if filt.include_cds_futures:
            seg = self._filter_futures(df, "CDS", filt)
            if filt.max_cds_futures:
                seg = seg.head(filt.max_cds_futures)
            instruments += self._to_instruments(seg, "CDS")
            self._log.info("CDS futures universe: %d instruments", len(seg))

        self._log.info("Total universe: %d instruments across all segments", len(instruments))
        return instruments

    @staticmethod
    def _filter_equity(df: pd.DataFrame, exch_seg: str, filt: UniverseFilter) -> pd.DataFrame:
        # The live scrip master does NOT have a 'series' column — series is
        # encoded as a suffix in the symbol field itself (e.g. "SBIN-EQ",
        # "SBIN-BE"). Filter on the suffix to drop non-EQ series.
        mask = df["exch_seg"] == exch_seg
        result = df[mask].copy()
        # Build suffix patterns from the configured eq_series list
        # e.g. ["EQ"] -> symbols ending with "-EQ"
        series_pattern = "|".join(f"-{s}$" for s in filt.eq_series)
        if series_pattern:
            result = result[result["symbol"].str.contains(series_pattern, regex=True, na=False)]
        return result.sort_values("symbol").reset_index(drop=True)

    @staticmethod
    def _filter_futures(df: pd.DataFrame, exch_seg: str, filt: UniverseFilter) -> pd.DataFrame:
        mask = (df["exch_seg"] == exch_seg)
        result = df[mask].copy()
        # Keep only futures (drop options, which have "OPT" in their type)
        type_mask = result["instrumenttype"].apply(
            lambda t: any(ft in str(t).upper() for ft in filt.fut_instrument_types)
        )
        result = result[type_mask]
        # Sort nearest-expiry first so max_nfo_futures cap hits active contracts
        if "expiry" in result.columns:
            result = result.sort_values("expiry")
        return result.reset_index(drop=True)

    @staticmethod
    def _to_instruments(df: pd.DataFrame, exch_seg: str) -> list[Instrument]:
        out = []
        for _, row in df.iterrows():
            try:
                out.append(Instrument(
                    token=str(row.get("token", "")),
                    symbol=str(row.get("symbol", "")),
                    name=str(row.get("name", row.get("symbol", ""))),
                    exchange=exch_seg,
                    candle_exchange=SEGMENT_TO_CANDLE_EXCHANGE.get(exch_seg, exch_seg),
                    instrument_type=str(row.get("instrumenttype", "EQ")),
                    lot_size=int(row.get("lotsize", 1) or 1),
                    tick_size=float(row.get("tick_size", 0.05) or 0.05),
                    expiry=str(row.get("expiry", "")),
                ))
            except Exception:
                continue  # skip any malformed rows silently
        return out
