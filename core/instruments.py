"""
core/instruments.py
Scrip master loader. AngelOne does not provide a per-symbol lookup
endpoint against the full master list — you pull the entire file and
filter locally. It only needs refreshing about once a day; cache to
disk so the main loop never re-downloads it on every restart.
"""
from __future__ import annotations
from pathlib import Path
import json
import time
import requests
import pandas as pd

SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


class InstrumentMaster:
    def __init__(self, cache_dir: Path, logger):
        self._cache_path = cache_dir / "scrip_master.json"
        self._log = logger
        self._df: pd.DataFrame | None = None

    def load(self, force_refresh: bool = False) -> pd.DataFrame:
        if not force_refresh and self._is_cache_fresh():
            self._df = pd.read_json(self._cache_path)
            self._log.info("Loaded scrip master from cache (%d rows)", len(self._df))
            return self._df

        self._log.info("Fetching fresh scrip master from AngelOne...")
        resp = requests.get(SCRIP_MASTER_URL, timeout=30)
        resp.raise_for_status()
        records = resp.json()

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(records))

        self._df = pd.DataFrame.from_records(records)
        self._log.info("Scrip master refreshed (%d rows)", len(self._df))
        return self._df

    def _is_cache_fresh(self) -> bool:
        if not self._cache_path.exists():
            return False
        age = time.time() - self._cache_path.stat().st_mtime
        return age < CACHE_MAX_AGE_SECONDS

    def lookup_token(self, symbol: str, exchange: str = "NSE") -> str:
        if self._df is None:
            self.load()
        match = self._df[(self._df["symbol"] == symbol) & (self._df["exch_seg"] == exchange)]
        if match.empty:
            raise KeyError(f"No instrument found for symbol={symbol} exchange={exchange}")
        return str(match.iloc[0]["token"])
