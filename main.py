"""
main.py
Production main loop. Key improvements over the previous version:

1. PRE-MARKET WARM-UP: fetches data and warms indicators at 09:10
   so the bot is ready the instant the 09:20 window opens, not
   computing indicators cold on the first live bar.

2. STOP/TARGET WIRING: RiskManager.check_open_trade() is called
   every bar. Stops and targets are enforced bar-by-bar using the
   high/low of each candle, not just the close. A spike through the
   stop and back above it still closes the trade.

3. API FAILURE CIRCUIT BREAKER: consecutive candle-fetch failures
   trigger a progressively longer sleep (up to 5 minutes) rather
   than hammering the endpoint. 5 consecutive failures = 5-minute
   pause + log alert.

4. CLEAN SQUAREOFF: at 15:20 the bot fetches a fresh price from the
   API rather than using the potentially stale last_price variable.

5. SIGNAL USES ATR: the RiskManager receives the current ATR from
   the indicator frame so position sizing is live, not a config
   default.
"""
from __future__ import annotations
import time
from datetime import datetime, timedelta

from config.settings import load_settings
from core.logger import get_logger
from core.auth import AngelOneSession
from core.instruments import InstrumentMaster
from core.data_feed import HistoricalDataFeed
from core.indicator_engine import IndicatorEngine, IndicatorConfig
from core.strategy import Strategy, Action
from core.risk_manager import RiskManager
from core.portfolio import Portfolio
from core.execution import ExecutionEngine
from core.excel_logger import ExcelLogger
from core.charges import ProductType
from core.market_hours import (
    MarketStatus, market_status, wait_for_market_open, now_ist, is_trading_day
)
from strategies.trend_momentum import TrendMomentumStrategy

# ── Bot config ────────────────────────────────────────────────────────────────
SYMBOL           = "SBIN-EQ"
EXCHANGE         = "NSE"
INTERVAL         = "FIVE_MINUTE"
LOOKBACK_DAYS    = 5
POLL_SECONDS     = 60            # polling cadence during market hours
PRODUCT_TYPE     = ProductType.INTRADAY
STRATEGY_NAME    = "TrendMomentum"
MAX_API_FAILURES = 5             # consecutive failures before 5-min pause
INDICATOR_CFG    = IndicatorConfig(fib_mode="static")


def _fetch_enriched(feed, engine, symbol_token, interval, lookback_days):
    """Fetch candles and return the enriched indicator frame."""
    to_date   = datetime.now()
    from_date = to_date - timedelta(days=lookback_days)
    raw = feed.get_candles(symbol_token, EXCHANGE, interval, from_date, to_date)
    return engine.transform(raw)


def run() -> None:
    settings = load_settings()
    logger = get_logger("algobot", settings.base_dir / "logs", settings.log_level)
    logger.info("=" * 65)
    logger.info("AngelOne AlgoBot | mode=%-6s | symbol=%s", settings.trading_mode, SYMBOL)
    logger.info("Capital=%.0f | risk_per_trade=%.1f%% | stop_mult=%.1fx | target_mult=%.1fx",
                settings.risk.account_capital, settings.risk.risk_per_trade_pct,
                settings.risk.atr_stop_multiplier, settings.risk.atr_target_multiplier)
    logger.info("=" * 65)

    # ── Wait until close to market open, then auth ────────────────────────
    wait_for_market_open(logger)

    session = AngelOneSession(settings.creds, logger)
    session.login()

    instruments = InstrumentMaster(settings.base_dir / "data" / "instruments", logger)
    symbol_token = instruments.lookup_token(SYMBOL, EXCHANGE)

    feed       = HistoricalDataFeed(session.client, logger)
    engine     = IndicatorEngine(INDICATOR_CFG)
    strategy: Strategy = TrendMomentumStrategy()
    risk       = RiskManager(settings.risk, logger)
    portfolio  = Portfolio(
        settings.initial_paper_capital,
        settings.base_dir / "data" / f"{settings.trading_mode.lower()}_trades.csv",
        logger,
    )
    excel_log  = ExcelLogger(
        data_dir=settings.base_dir / "data",
        product_type=PRODUCT_TYPE,
        brokerage_flat=settings.costs.commission_per_order,
    )
    excel_path = excel_log.open_session()
    logger.info("Trade log: %s", excel_path)

    execution = ExecutionEngine(
        smart_connect_client=session.client,
        trading_mode=settings.trading_mode,
        exchange=EXCHANGE,
        costs=settings.costs,
        portfolio=portfolio,
        logger=logger,
        excel_logger=excel_log,
        product_type=PRODUCT_TYPE,
        strategy_name=STRATEGY_NAME,
    )

    # ── Pre-market warm-up (fetch data before 09:20 window opens) ─────────
    logger.info("Pre-market warm-up: fetching historical data to warm indicators...")
    try:
        enriched = _fetch_enriched(feed, engine, symbol_token, INTERVAL, LOOKBACK_DAYS)
        logger.info("Warm-up complete: %d bars loaded, last close=%.2f",
                    len(enriched), enriched["close"].iloc[-1])
    except Exception:
        logger.exception("Warm-up fetch failed — will retry in main loop")
        enriched = None

    last_date          = None
    squareoff_done     = False
    consecutive_fails  = 0
    last_price         = None

    try:
        while True:
            status = market_status()
            now    = now_ist()
            today  = now.date()

            # ── Daily reset on date change ─────────────────────────────────
            if last_date is not None and today != last_date:
                logger.info("New trading day — resetting daily counters")
                risk.reset_daily()
                squareoff_done = False
                consecutive_fails = 0
            last_date = today

            # ── Market closed ──────────────────────────────────────────────
            if status == MarketStatus.CLOSED:
                logger.info("Market closed. Sleeping until next session.")
                wait_for_market_open(logger)
                continue

            # ── Pre-market ─────────────────────────────────────────────────
            if status == MarketStatus.PRE_MARKET:
                time.sleep(30)
                continue

            # ── Hard squareoff 15:20 ───────────────────────────────────────
            if status == MarketStatus.SQUAREOFF and not squareoff_done:
                logger.warning("15:20 — forcing squareoff of all open intraday positions")
                try:
                    session.ensure_fresh()
                    # Fetch a fresh price for squareoff — don't use stale last_price
                    fresh = _fetch_enriched(feed, engine, symbol_token, INTERVAL, 1)
                    sq_price = float(fresh["close"].iloc[-1])
                    fill = execution.force_squareoff(SYMBOL, symbol_token, sq_price)
                    if fill:
                        risk.record_fill(
                            fill.action, fill.quantity,
                            fill.price, SYMBOL, fill.realized_pnl,
                        )
                        logger.info("Squareoff complete | pnl=%.2f", fill.realized_pnl)
                except Exception:
                    logger.exception("Squareoff error — check broker terminal immediately")
                squareoff_done = True
                time.sleep(POLL_SECONDS)
                continue

            # ── Entry cutoff: 15:15–15:20 ─────────────────────────────────
            if status == MarketStatus.ENTRY_CUTOFF:
                # Still monitor stop/target on open positions
                if enriched is not None and risk._open_trade is not None:
                    last_bar = enriched.iloc[-1]
                    exit_intent = risk.check_open_trade(
                        SYMBOL, float(last_bar["high"]), float(last_bar["low"])
                    )
                    if exit_intent:
                        if last_price:
                            fill = execution.execute(exit_intent, SYMBOL, symbol_token, last_price,
                                                      notes="exit-cutoff stop/target")
                            risk.record_fill(fill.action, fill.quantity,
                                              fill.price, SYMBOL, fill.realized_pnl)
                time.sleep(POLL_SECONDS)
                continue

            # ── Active trading window 09:20–15:15 ─────────────────────────
            try:
                session.ensure_fresh()
                enriched  = _fetch_enriched(feed, engine, symbol_token, INTERVAL, LOOKBACK_DAYS)
                last_bar  = enriched.iloc[-1]
                last_price = float(last_bar["close"])
                atr        = float(last_bar["atr"]) if not __import__("math").isnan(float(last_bar.get("atr", float("nan")))) else 0.0
                consecutive_fails = 0  # reset on success

            except Exception:
                consecutive_fails += 1
                logger.exception("Candle fetch error (consecutive=%d)", consecutive_fails)
                if consecutive_fails >= MAX_API_FAILURES:
                    logger.error("API circuit breaker: %d consecutive failures — pausing 5 min", consecutive_fails)
                    time.sleep(300)
                    consecutive_fails = 0
                else:
                    time.sleep(POLL_SECONDS)
                continue

            # ── Check open trade stop/target first ─────────────────────────
            exit_intent = risk.check_open_trade(
                SYMBOL, float(last_bar["high"]), float(last_bar["low"])
            )
            if exit_intent:
                fill = execution.execute(exit_intent, SYMBOL, symbol_token, last_price,
                                          notes="stop/target hit")
                risk.record_fill(
                    fill.action, fill.quantity,
                    fill.price, SYMBOL, fill.realized_pnl,
                )
                logger.info(
                    "Stop/target exit | pnl=%.2f | cum_pnl=%.2f | consecutive_losses=%d",
                    fill.realized_pnl, risk.realized_pnl_today, risk.consecutive_losses,
                )
                time.sleep(POLL_SECONDS)
                continue

            # ── Strategy signal ────────────────────────────────────────────
            signal  = strategy.generate_signal(enriched)
            intent  = risk.evaluate(signal, last_price, atr)

            logger.info(
                "[%s] %s | conf=%.2f | atr=%.2f | pos=%d | pnl_today=%.2f | reason: %s",
                now.strftime("%H:%M"), signal.action.value, signal.confidence,
                atr, risk.current_position_qty, risk.realized_pnl_today,
                signal.reason[:80],
            )

            if intent:
                fill = execution.execute(intent, SYMBOL, symbol_token, last_price)
                risk.record_fill(
                    fill.action, fill.quantity,
                    fill.price, SYMBOL, fill.realized_pnl,
                    stop_price=fill.stop_price,
                    target_price=fill.target_price,
                )
                logger.info(
                    "FILL | %s %d x %s @ %.2f | stop=%.2f | target=%.2f | charges=%.2f",
                    fill.action.value, fill.quantity, SYMBOL, fill.price,
                    fill.stop_price, fill.target_price, fill.total_charges,
                )

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        prices = {SYMBOL: last_price} if last_price else {}
        logger.info(
            "SESSION END | cash=%.2f | position=%d | realized_pnl=%.2f | equity≈%.2f | trades=%d",
            portfolio.cash,
            portfolio.get_position(SYMBOL).quantity,
            risk.realized_pnl_today,
            portfolio.equity(prices),
            risk.trades_today,
        )
        excel_log.close()
        logger.info("Excel log saved: %s", excel_path)
        print(f"\nDone. Trade log: {excel_path}")


if __name__ == "__main__":
    run()
