"""
core/charges.py
Indian market transaction cost calculator — AngelOne tariff structure.

Every rupee of charges is P&L drag. Backtests that ignore these are
lying to you. This module computes the REAL all-in cost per trade,
split into the components your tracker expects: Broker ₹, STT ₹, Other ₹.

Rates current as of FY2025. SEBI revised charges in October 2023;
STT on F&O options was raised to 0.1% (sell side on premium) in
Budget 2024 — update this file when the next budget hits.

AngelOne brokerage:
  - Equity delivery: ₹0 (zero brokerage) or ₹20 — AngelOne is ₹0
    for delivery but to be conservative we use ₹20 per leg here.
  - Equity intraday / F&O: ₹20 flat per order, both sides.
  Use the .env COMMISSION_PER_ORDER to override if you're on a
  different plan or negotiated rate.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class ProductType(Enum):
    INTRADAY    = "INTRADAY"    # equity intraday / MIS
    DELIVERY    = "DELIVERY"    # equity CNC
    FO_FUTURES  = "FO_FUTURES"  # index + stock futures
    FO_OPTIONS  = "FO_OPTIONS"  # index + stock options (on premium)
    CURRENCY    = "CURRENCY"    # CDS currency derivatives
    COMMODITY   = "COMMODITY"   # MCX futures


@dataclass(frozen=True)
class TradeCost:
    brokerage: float
    stt: float
    exchange_txn: float
    sebi_charges: float
    stamp_duty: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange_txn + self.sebi_charges + self.stamp_duty + self.gst

    @property
    def other(self) -> float:
        """'Other ₹' column in the tracker = exchange + SEBI + stamp + GST."""
        return self.exchange_txn + self.sebi_charges + self.stamp_duty + self.gst


# ── Per-side rate tables ─────────────────────────────────────────────────────
# (rate, applies_to) where applies_to is "buy", "sell", or "both"

_STT_RATES = {
    ProductType.INTRADAY:   (0.00025,  "sell"),   # 0.025%
    ProductType.DELIVERY:   (0.001,    "both"),   # 0.1%
    ProductType.FO_FUTURES: (0.0001,   "sell"),   # 0.01%  (was 0.0125% until FY24 budget)
    ProductType.FO_OPTIONS: (0.001,    "sell"),   # 0.1% on premium (post-Budget 2024)
    ProductType.CURRENCY:   (0.0,      "none"),   # NIL for CDS
    ProductType.COMMODITY:  (0.0001,   "sell"),   # 0.01% (MCX futures)
}

_TXN_RATES = {   # NSE/BSE exchange transaction charges on turnover
    ProductType.INTRADAY:   0.0000335,  # 0.00335%
    ProductType.DELIVERY:   0.0000335,
    ProductType.FO_FUTURES: 0.00000188, # 0.000188% — MUCH lower than equity
    ProductType.FO_OPTIONS: 0.00035,    # 0.035% on premium
    ProductType.CURRENCY:   0.000009,   # 0.0009%
    ProductType.COMMODITY:  0.0000026,  # 0.00026%
}

_STAMP_RATES = {  # charged on BUY side only
    ProductType.INTRADAY:   0.00003,   # 0.003% on turnover
    ProductType.DELIVERY:   0.00015,   # 0.015%
    ProductType.FO_FUTURES: 0.00002,   # 0.002%
    ProductType.FO_OPTIONS: 0.00003,   # 0.003% on premium
    ProductType.CURRENCY:   0.00001,   # 0.001%
    ProductType.COMMODITY:  0.00002,   # 0.002%
}

_SEBI_RATE = 0.0000001  # ₹10 per crore = 1e-7 on turnover
_GST_RATE  = 0.18        # 18% on (brokerage + exchange txn charges)


def calculate_charges(
    price: float,
    quantity: int,
    product_type: ProductType,
    side: str,             # "BUY" or "SELL"
    brokerage_flat: float = 20.0,
) -> TradeCost:
    """
    Returns a TradeCost breakdown for one order leg.

    For a complete round-trip (buy + sell), call this twice and sum
    the fields. The caller (execution.py / backtester.py) is
    responsible for deciding which product_type applies — intraday
    trades opened and closed same-day are INTRADAY; anything held
    overnight is DELIVERY.
    """
    turnover = price * quantity  # total order value in ₹

    # STT — side-specific
    stt_rate, stt_side = _STT_RATES[product_type]
    if stt_side == "both" or stt_side == side.lower():
        stt = turnover * stt_rate
    else:
        stt = 0.0

    # Exchange transaction charges
    txn = turnover * _TXN_RATES[product_type]

    # SEBI charges (tiny but non-zero)
    sebi = turnover * _SEBI_RATE

    # Stamp duty — BUY side only
    stamp = turnover * _STAMP_RATES[product_type] if side.upper() == "BUY" else 0.0

    # GST on brokerage + exchange charges (not on STT/stamp/SEBI)
    gst = (brokerage_flat + txn) * _GST_RATE

    return TradeCost(
        brokerage=brokerage_flat,
        stt=round(stt, 4),
        exchange_txn=round(txn, 4),
        sebi_charges=round(sebi, 4),
        stamp_duty=round(stamp, 4),
        gst=round(gst, 4),
    )


def round_trip_charges(
    entry_price: float,
    exit_price: float,
    quantity: int,
    product_type: ProductType,
    brokerage_flat: float = 20.0,
) -> TradeCost:
    """Sum of buy-leg + sell-leg charges for reporting on a closed trade."""
    buy_leg  = calculate_charges(entry_price, quantity, product_type, "BUY",  brokerage_flat)
    sell_leg = calculate_charges(exit_price,  quantity, product_type, "SELL", brokerage_flat)
    return TradeCost(
        brokerage    = buy_leg.brokerage + sell_leg.brokerage,
        stt          = buy_leg.stt       + sell_leg.stt,
        exchange_txn = buy_leg.exchange_txn + sell_leg.exchange_txn,
        sebi_charges = buy_leg.sebi_charges + sell_leg.sebi_charges,
        stamp_duty   = buy_leg.stamp_duty   + sell_leg.stamp_duty,
        gst          = buy_leg.gst          + sell_leg.gst,
    )
