"""
strategies/trend_momentum.py
Production-grade intraday trend strategy for Indian cash equities.

Core logic:
  - LONG ONLY. On NSE cash segment, shorting is not available for most
    retail accounts without F&O access and brings margin complications.
    A missed short opportunity costs you nothing; a failed short order
    can leave your position state wrong.

  - ENTRY conditions (all must be true simultaneously):
      1. Price > SMA20 (trend filter — don't fight the trend)
      2. SMA20 slope positive for last 3 bars (trend accelerating, not rolling over)
      3. MACD histogram just crossed above zero from below (momentum trigger)
      4. MACD histogram has been negative for at least 2 bars before crossing
         (avoids false signals from noise around the zero line)
      5. Bollinger %B between 0.35 and 0.80 (not oversold, not overbought —
         avoid chasing at the top or catching a falling knife)
      6. Relative volume >= 1.2x the 20-bar average (liquidity confirmation —
         a breakout on thin volume almost always fails in Indian markets)
      7. ATR is non-NaN (indicators are fully warmed up)

  - EXIT conditions (checked every bar while in a long):
      ATR-based stop and target are computed at entry and tracked by
      the risk manager. The strategy itself does NOT manage exits —
      that's the risk manager's job, because it has better context
      (entry price, P&L, consecutive losses). Strategy only signals
      HOLD while in a position to prevent re-entry into an existing trade.

  - SELL action is never generated for a fresh short. The only
    sell-side signal is EXIT (close the current long).

Why these specific filters:
  - Indian 5-minute bars during 9:20–10:30 are extremely noisy. The
    SMA slope + volume filters eliminate most false crossovers.
  - Bollinger %B 0.35–0.80 keeps entries near the middle of the band
    rather than at extremes, which are mean-reverting on 5-min.
  - The "histogram negative for 2+ bars before crossing" filter is the
    single most important noise reduction — it ensures the cross is
    real, not a one-bar wick that flips back immediately.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from core.strategy import Strategy, Signal, Action

REQUIRED = ["sma_20", "sma_slope", "macd_hist", "bb_percent_b", "atr", "rel_volume"]


class TrendMomentumStrategy(Strategy):
    def __init__(
        self,
        bb_entry_min: float = 0.35,   # don't buy oversold
        bb_entry_max: float = 0.80,   # don't chase at the top
        min_rel_volume: float = 1.2,  # must be 1.2x avg volume
        macd_neg_bars_min: int = 2,   # histogram negative for at least this many bars before cross
    ):
        self._bb_min = bb_entry_min
        self._bb_max = bb_entry_max
        self._min_vol = min_rel_volume
        self._macd_neg_min = macd_neg_bars_min

    def generate_signal(self, df: pd.DataFrame) -> Signal:
        if len(df) < 5:
            return Signal(Action.HOLD, 0.0, "insufficient bars")

        last = df.iloc[-1]

        # Warm-up check
        if last[REQUIRED].isna().any():
            return Signal(Action.HOLD, 0.0, "indicators warming up")

        # ── EXIT checks (checked before any entry logic) ──────────────────
        # Stop and target are managed externally by RiskManager.
        # Strategy signals EXIT only on a clear technical breakdown:
        # price crosses back below SMA20 while we're in a long.
        # (Actual stop/target enforcement is in RiskManager.check_open_trade)

        # ── TREND FILTER ──────────────────────────────────────────────────
        price_above_sma = last["close"] > last["sma_20"]
        sma_slope_positive = last["sma_slope"] > 0

        if not (price_above_sma and sma_slope_positive):
            return Signal(Action.HOLD, 0.0, "trend filter: price below SMA20 or slope flat")

        # ── MACD MOMENTUM ─────────────────────────────────────────────────
        hist = df["macd_hist"].values
        # Current bar just crossed above zero
        macd_just_crossed = hist[-2] < 0 and hist[-1] > 0

        # How many consecutive negative bars preceded the cross?
        neg_bars_before = 0
        for i in range(len(hist) - 2, -1, -1):
            if hist[i] < 0:
                neg_bars_before += 1
            else:
                break

        if not macd_just_crossed:
            return Signal(Action.HOLD, 0.0, "no MACD bullish cross this bar")

        if neg_bars_before < self._macd_neg_min:
            return Signal(
                Action.HOLD, 0.0,
                f"MACD cross too soon — only {neg_bars_before} negative bars before cross (need {self._macd_neg_min})"
            )

        # ── BOLLINGER BAND ZONE ───────────────────────────────────────────
        pct_b = float(last["bb_percent_b"])
        if not (self._bb_min <= pct_b <= self._bb_max):
            return Signal(
                Action.HOLD, 0.0,
                f"BB %B={pct_b:.2f} outside entry zone [{self._bb_min},{self._bb_max}]"
            )

        # ── VOLUME CONFIRMATION ───────────────────────────────────────────
        rel_vol = float(last["rel_volume"])
        if np.isnan(rel_vol) or rel_vol < self._min_vol:
            return Signal(
                Action.HOLD, 0.0,
                f"volume too thin: rel_volume={rel_vol:.2f} < {self._min_vol}"
            )

        # ── ALL CONDITIONS MET ────────────────────────────────────────────
        # Confidence is a weighted score, not a magic number. RiskManager
        # uses it to make minor qty adjustments around a fixed-risk base.
        # It is NOT the primary sizing driver — ATR + risk_pct is.
        confidence = round(
            0.35                                          # base for passing all filters
            + min(0.20, (pct_b - self._bb_min) / (self._bb_max - self._bb_min) * 0.20)  # better zone = higher
            + min(0.25, (rel_vol - self._min_vol) / 2.0 * 0.25)  # higher volume = more confident
            + min(0.20, neg_bars_before / 5.0 * 0.20),   # cleaner pullback = more confident
            2,
        )
        confidence = min(0.95, confidence)

        return Signal(
            Action.BUY,
            confidence=confidence,
            reason=(
                f"Trend+MACD+Vol entry | sma_slope={last['sma_slope']:.3f} "
                f"bb_pct={pct_b:.2f} rel_vol={rel_vol:.2f} "
                f"neg_bars={neg_bars_before} atr={last['atr']:.2f}"
            ),
        )
