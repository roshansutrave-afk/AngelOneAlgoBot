# AngelOne AlgoBot

Modular algorithmic trading bot for AngelOne's SmartAPI. Pipeline:
SmartAPI auth -> data feed -> indicator engine -> strategy -> risk
manager -> portfolio/execution, with PAPER mode as the default so
nothing touches real capital until you explicitly flip it. Now
includes a bar-by-bar backtester with realistic fill timing/costs,
and a Tkinter GUI launcher so you're not juggling terminal windows.

## Setup

1. **Create a SmartAPI app & enable TOTP**
   - Register at https://smartapi.angelone.in and create an app to get
     your `ANGEL_API_KEY`.
   - Enable TOTP-based login at the AngelOne TOTP setup page (search
     "AngelOne enable TOTP" if the URL has moved) and save the secret
     shown during QR setup — that's your `ANGEL_TOTP_SECRET`.

2. **Install dependencies**
   ```
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```
   Use `python -m pip install` rather than bare `pip` if your machine
   has more than one Python install — `pip` and `python` can silently
   point at different interpreters (this bit us during development;
   `python -m pip` guarantees they match).

3. **Configure credentials**
   ```
   copy .env.example .env
   ```
   Fill in the AngelOne credentials. Leave `TRADING_MODE=PAPER`. Set
   `SLIPPAGE_BPS` / `COMMISSION_PER_ORDER` to something realistic for
   your broker plan — check AngelOne's current tariff sheet, the
   defaults here are placeholders, not verified current pricing.

4. **Run the test suite**
   ```
   python -m pytest tests/ -v
   ```

5. **Either drive everything from the GUI...**
   ```
   python gui_launcher.py
   ```
   Buttons for: start/stop the paper trading bot, run a backtest
   (against a local CSV or live AngelOne historical data), run tests,
   open `.env`, open the logs folder, open the last equity curve PNG —
   all with live streamed output in the window. It's a process
   launcher + log viewer, not a charting dashboard.

   **...or from the command line:**
   ```
   python main.py
   python backtest_runner.py --csv data\sample_sbin_daily.csv
   python backtest_runner.py --symbol SBIN-EQ --exchange NSE --interval ONE_DAY --days 365
   ```

## Architecture

- `config/settings.py` — env-var driven config (credentials, risk limits, execution costs).
- `core/auth.py` — SmartConnect session + TOTP login, defensive token refresh.
- `core/instruments.py` — scrip master download/cache, symbol -> token lookup.
- `core/data_feed.py` — `HistoricalDataFeed` (REST candles) and `LiveTickFeed`
  (SmartWebSocketV2 wrapper — built, not yet wired into `main.py`).
- `core/indicator_engine.py` — SMA, Bollinger Bands, MACD, Fibonacci retracements.
  Two Fibonacci modes: `"static"` (live — correct, since "now" has no future
  to leak) and `"rolling"` (backtest — causal, recomputed at every bar).
- `core/strategy.py` / `strategies/trend_momentum.py` — strategy interface + baseline.
- `core/risk_manager.py` — position limits, daily loss kill switch, sizing.
- `core/portfolio.py` — average-cost-basis position/P&L ledger, persisted to CSV.
- `core/execution.py` — PAPER (simulated, slippage+commission)/LIVE-gated order placement.
- `core/backtester.py` — bar-by-bar engine: next-bar-open fills, slippage,
  commission, daily kill-switch resets, full metrics (Sharpe, CAGR, max
  drawdown, win rate, profit factor).
- `backtest_runner.py` — CLI: run a backtest, save trades.csv/equity_curve.csv/
  metrics.json/equity_curve.png.
- `gui_launcher.py` — Tkinter control panel wrapping the two CLIs above.
- `main.py` — live polling loop tying the whole pipeline together.

## Bugs fixed during this build (worth knowing about)

- **The daily-loss kill switch was a no-op.** `main.py` was calling
  `risk.record_fill(action, quantity)` with no `pnl_delta`, so
  `RiskManager.realized_pnl_today` never moved and `max_daily_loss`
  could never trigger. Fixed by routing every fill through
  `Portfolio.apply_fill()` (real average-cost-basis P&L) and passing
  `fill.realized_pnl` into `record_fill()`. Covered by
  `tests/test_backtester.py::test_realized_pnl_flows_into_kill_switch`
  and a negative-control test proving the old pattern really was broken.
- **Look-ahead bias in Fibonacci levels for backtesting.** The original
  engine computed one swing high/low over the whole frame — fine for
  live trading (the end of the frame is always "now"), wrong for a
  static historical frame (bar 1 could be influenced by a swing 3 years
  later). Fixed with `fib_mode="rolling"`, used exclusively by the
  backtester. Covered by `test_rolling_fib_has_no_lookahead`.
- **Fill timing optimism.** Backtester fills at the *next* bar's open,
  not the signal bar's close — you can't actually transact at the exact
  close the instant a signal fires.
- **Silent trade-reporting gap.** A position still open when a backtest
  ends used to vanish from `result.trades` entirely (only closed trades
  were appended). Now flushed at end-of-run as an open (unrealized) trade.

## Known gaps / next steps

- `TrendMomentumStrategy` has now been backtested with realistic costs —
  but only against synthetic random-walk data. Validate against real
  historical data for your actual instrument/timeframe before sizing
  real capital against it.
- `LiveTickFeed`/`SmartWebSocketV2` is built but not wired into `main.py`'s
  loop — the polling approach is simpler to validate first.
- `core/execution.py`'s LIVE branch uses `last_price` as an approximation
  of the actual fill price for MARKET orders — reconcile against
  AngelOne's trade book / order history API before trusting realized
  P&L numbers in LIVE mode.
- No CNN pattern-recognition or RL layers yet — Phase 2/3 of the original
  roadmap, intended to slot in as additional state-vector features for a
  new `Strategy` implementation, not replacements for this one.
- `IndicatorEngine._fibonacci_rolling` is an O(n × lookback) Python loop —
  fine for single backtests up to tens of thousands of bars; vectorize
  with numpy stride tricks or numba before running it inside a large
  hyperparameter sweep.
- Hard requirement before LIVE: the kill switch is now real — prove it
  to yourself by setting `MAX_DAILY_LOSS` artificially low and watching
  PAPER mode actually halt.

`_gen_sample_data.py` at the repo root generates a synthetic OHLCV CSV
for smoke-testing `backtest_runner.py` without AngelOne credentials —
not part of the bot, safe to delete or keep around for quick checks.
