"""
core/market_hours.py
IST-aware market hours guard for NSE/BSE.

Hard rules baked in from actual Indian market practice:
  - Regular session: 09:15 - 15:30 IST, Mon-Fri
  - Bot doesn't enter new positions before 09:20 (first 5 min is 
    pure noise — order book is thin, spreads are wide, algos are
    fighting each other. The tracker's own Golden Rule #7 says this.)
  - Bot stops new entries at 15:15 (15-min buffer before close).
  - Hard squareoff trigger fires at 15:20 — AngelOne auto-squares
    intraday at 15:25, so we want to be out before that happens to
    avoid getting filled at the worst possible price.
  - Pre-market: bot sleeps with countdown and re-checks every minute.
  - Post-market / weekend: returns status so main.py can skip
    gracefully instead of hammering the API with pointless calls.

NSE holiday list is maintained as a frozenset of date strings 
(YYYY-MM-DD). Update NSE_HOLIDAYS each year — the exchange publishes
the full list in November for the next calendar year.
"""
from __future__ import annotations
from datetime import date, datetime, time
from enum import Enum, auto
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# NSE trading holidays 2025 — update annually
# Source: NSE circular (add BSE holidays too — they differ occasionally)
NSE_HOLIDAYS_2025: frozenset[date] = frozenset([
    date(2025, 2, 26),  # Mahashivratri
    date(2025, 3, 14),  # Holi
    date(2025, 3, 31),  # Id-ul-Fitr (Ramzan Id)
    date(2025, 4, 10),  # Shri Ram Navami
    date(2025, 4, 14),  # Dr. Baba Saheb Ambedkar Jayanti
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 1),   # Maharashtra Day
    date(2025, 8, 15),  # Independence Day
    date(2025, 8, 27),  # Ganesh Chaturthi
    date(2025, 10, 2),  # Mahatma Gandhi Jayanti
    date(2025, 10, 2),  # Gandhi Jayanti
    date(2025, 10, 24), # Diwali - Laxmi Pujan (Muhurat trading only)
    date(2025, 11, 5),  # Prakash Gurpurb Sri Guru Nanak Dev Ji
    date(2025, 12, 25), # Christmas
])

NSE_HOLIDAYS: frozenset[date] = NSE_HOLIDAYS_2025  # extend as needed

MARKET_OPEN  = time(9, 15, tzinfo=IST)
BOT_ENTRY_START = time(9, 20, tzinfo=IST)  # skip opening volatility
BOT_ENTRY_CUTOFF = time(15, 15, tzinfo=IST)  # no new entries after this
SQUAREOFF_TIME   = time(15, 20, tzinfo=IST)  # hard squareoff trigger
MARKET_CLOSE = time(15, 30, tzinfo=IST)


class MarketStatus(Enum):
    PRE_MARKET = auto()     # before 09:20, or holiday/weekend upcoming
    OPEN = auto()           # 09:20–15:15, active trading window
    ENTRY_CUTOFF = auto()   # 15:15–15:20, manage existing; no new entries
    SQUAREOFF = auto()      # 15:20–15:30, force-close all intraday positions
    CLOSED = auto()         # after 15:30 or holiday/weekend


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def is_trading_day(d: date | None = None) -> bool:
    d = d or now_ist().date()
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def market_status() -> MarketStatus:
    now = now_ist()
    if not is_trading_day(now.date()):
        return MarketStatus.CLOSED

    t = now.time().replace(tzinfo=IST)
    if t < BOT_ENTRY_START:
        return MarketStatus.PRE_MARKET
    if t < BOT_ENTRY_CUTOFF:
        return MarketStatus.OPEN
    if t < SQUAREOFF_TIME:
        return MarketStatus.ENTRY_CUTOFF
    if t < MARKET_CLOSE:
        return MarketStatus.SQUAREOFF
    return MarketStatus.CLOSED


def seconds_until_market_open() -> float:
    """Seconds until BOT_ENTRY_START on the next trading day."""
    now = now_ist()
    target = now.replace(hour=9, minute=20, second=0, microsecond=0)
    if target <= now or not is_trading_day(now.date()):
        # Move to next trading day
        next_day = now.date()
        for _ in range(10):
            import datetime as _dt
            next_day += _dt.timedelta(days=1)
            if is_trading_day(next_day):
                break
        target = datetime(next_day.year, next_day.month, next_day.day,
                           9, 20, 0, tzinfo=IST)
    return max(0.0, (target - now).total_seconds())


def wait_for_market_open(logger) -> None:
    """
    Block until BOT_ENTRY_START on the next trading day.
    Logs a countdown every 5 minutes so you know the bot is alive.
    """
    import time as _time
    while True:
        status = market_status()
        if status == MarketStatus.OPEN:
            return
        secs = seconds_until_market_open()
        if secs <= 0:
            return
        mins = int(secs // 60)
        logger.info("Market not open yet — %s | %d min until 09:20 IST. Sleeping...",
                    status.name, mins)
        _time.sleep(min(300, secs))  # wake up every 5 min max to re-check
