"""
core/data_feed.py
Two data paths:
  - Historical OHLCV via REST (getCandleData) — for backtesting & warm-up.
  - Live ticks via SmartWebSocketV2 — for the running strategy loop,
    wired in once the polling-based main loop is validated.

Candle data comes back as raw [timestamp, o, h, l, c, v] arrays; this
module's only job is turning that into the DatetimeIndex'd DataFrame
IndicatorEngine expects.
"""
from __future__ import annotations
from datetime import datetime
from typing import Callable
import pandas as pd
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

VALID_INTERVALS = {
    "ONE_MINUTE", "THREE_MINUTE", "FIVE_MINUTE", "TEN_MINUTE",
    "FIFTEEN_MINUTE", "THIRTY_MINUTE", "ONE_HOUR", "ONE_DAY",
}


class HistoricalDataFeed:
    def __init__(self, smart_connect_client, logger):
        self._client = smart_connect_client
        self._log = logger

    def get_candles(
        self,
        symbol_token: str,
        exchange: str,
        interval: str,
        from_date: datetime,
        to_date: datetime,
    ) -> pd.DataFrame:
        if interval not in VALID_INTERVALS:
            raise ValueError(f"interval must be one of {VALID_INTERVALS}, got {interval}")

        params = {
            "exchange": exchange,
            "symboltoken": symbol_token,
            "interval": interval,
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M"),
        }
        resp = self._client.getCandleData(params)
        if not resp.get("status"):
            raise RuntimeError(f"getCandleData failed: {resp}")

        rows = resp["data"]  # [[ts, o, h, l, c, v], ...]
        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()
        self._log.info("Fetched %d candles for token=%s (%s)", len(df), symbol_token, interval)
        return df


class LiveTickFeed:
    """
    Thin wrapper around SmartWebSocketV2 with a clean callback interface.
    Constants mirror the SDK's own (NSE_CM=1, NSE_FO=2, BSE_CM=3, BSE_FO=4,
    MCX_FO=5, NCX_FO=7, CDE_FO=13; LTP=1, QUOTE=2, SNAP_QUOTE=3, DEPTH=4),
    confirmed against the SmartApi source — verify against current SDK
    if these ever shift.

    NOTE: last_traded_price in tick messages is in paise — divide by 100
    before using it as a price in your strategy/risk logic.
    """

    EXCHANGE_TYPE = {"NSE_CM": 1, "NSE_FO": 2, "BSE_CM": 3, "BSE_FO": 4,
                      "MCX_FO": 5, "NCX_FO": 7, "CDE_FO": 13}
    MODE = {"LTP": 1, "QUOTE": 2, "SNAP_QUOTE": 3, "DEPTH": 4}

    def __init__(self, jwt_token: str, api_key: str, client_code: str, feed_token: str, logger):
        self._log = logger
        self._ws = SmartWebSocketV2(jwt_token, api_key, client_code, feed_token)
        self._on_tick: Callable[[dict], None] | None = None
        self._subscriptions: list[dict] = []

        self._ws.on_open = self._handle_open
        self._ws.on_data = self._handle_data
        self._ws.on_error = self._handle_error
        self._ws.on_close = self._handle_close

    def subscribe(self, correlation_id: str, mode: str, exchange_type: str, tokens: list[str]) -> None:
        self._subscriptions.append({
            "correlation_id": correlation_id,
            "mode": self.MODE[mode],
            "token_list": [{"exchangeType": self.EXCHANGE_TYPE[exchange_type], "tokens": tokens}],
        })

    def on_tick(self, callback: Callable[[dict], None]) -> None:
        self._on_tick = callback

    def connect(self) -> None:
        self._ws.connect()  # blocking — run in its own thread from main.py

    def _handle_open(self, wsapp) -> None:
        self._log.info("WebSocket connected — sending %d subscription(s)", len(self._subscriptions))
        for sub in self._subscriptions:
            self._ws.subscribe(sub["correlation_id"], sub["mode"], sub["token_list"])

    def _handle_data(self, wsapp, message) -> None:
        if self._on_tick:
            self._on_tick(message)

    def _handle_error(self, wsapp, error) -> None:
        self._log.error("WebSocket error: %s", error)

    def _handle_close(self, wsapp) -> None:
        self._log.warning("WebSocket closed")
