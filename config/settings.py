"""
config/settings.py
Centralized configuration. Every risk parameter that matters is
here — not scattered across strategy files or hardcoded in main.py.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Missing required .env key: {key}")
    return val


def _get(key: str, default: str) -> str:
    return os.getenv(key, default)


@dataclass(frozen=True)
class AngelOneCredentials:
    api_key: str
    client_code: str
    pin: str
    totp_secret: str


@dataclass(frozen=True)
class RiskConfig:
    # Capital and daily limits
    account_capital: float      # total trading capital — used for ATR sizing
    max_daily_loss: float       # hard kill-switch threshold (₹)
    max_daily_loss_pct: float   # same limit as % of capital (whichever trips first)
    max_trades_per_day: int     # stop entering new trades beyond this count

    # Per-trade risk
    risk_per_trade_pct: float   # % of capital risked per trade (drives qty sizing)
    atr_stop_multiplier: float  # stop = entry ± (ATR × this)
    atr_target_multiplier: float# target = entry ± (ATR × this). Must be > stop mult
    min_risk_reward: float      # veto any trade where R:R < this

    # Position limits
    max_position_qty: int       # hard cap on shares regardless of ATR sizing
    allow_shorts: bool          # False = long-only (safest for cash equity)

    # Consecutive loss protection
    max_consecutive_losses: int # pause new entries if this many losses in a row


@dataclass(frozen=True)
class ExecutionCosts:
    slippage_bps: float
    commission_per_order: float


@dataclass(frozen=True)
class Settings:
    creds: AngelOneCredentials
    risk: RiskConfig
    costs: ExecutionCosts
    trading_mode: str
    log_level: str
    initial_paper_capital: float
    base_dir: Path = BASE_DIR


def load_settings() -> Settings:
    creds = AngelOneCredentials(
        api_key=_require("ANGEL_API_KEY"),
        client_code=_require("ANGEL_CLIENT_CODE"),
        pin=_require("ANGEL_PASSWORD_OR_PIN"),
        totp_secret=_require("ANGEL_TOTP_SECRET"),
    )

    capital = float(_get("ACCOUNT_CAPITAL", "100000"))
    max_daily_loss = float(_get("MAX_DAILY_LOSS", "2000"))

    risk = RiskConfig(
        account_capital=capital,
        max_daily_loss=max_daily_loss,
        max_daily_loss_pct=float(_get("MAX_DAILY_LOSS_PCT", "2.0")),
        max_trades_per_day=int(_get("MAX_TRADES_PER_DAY", "5")),
        risk_per_trade_pct=float(_get("RISK_PER_TRADE_PCT", "0.5")),
        atr_stop_multiplier=float(_get("ATR_STOP_MULTIPLIER", "1.5")),
        atr_target_multiplier=float(_get("ATR_TARGET_MULTIPLIER", "2.5")),
        min_risk_reward=float(_get("MIN_RISK_REWARD", "1.5")),
        max_position_qty=int(_get("MAX_POSITION_QTY", "500")),
        allow_shorts=_get("ALLOW_SHORTS", "false").lower() == "true",
        max_consecutive_losses=int(_get("MAX_CONSECUTIVE_LOSSES", "3")),
    )

    costs = ExecutionCosts(
        slippage_bps=float(_get("SLIPPAGE_BPS", "5.0")),
        commission_per_order=float(_get("COMMISSION_PER_ORDER", "20.0")),
    )

    trading_mode = _get("TRADING_MODE", "PAPER").upper()
    if trading_mode not in {"PAPER", "LIVE"}:
        raise ValueError(f"TRADING_MODE must be PAPER or LIVE, got: {trading_mode}")

    return Settings(
        creds=creds,
        risk=risk,
        costs=costs,
        trading_mode=trading_mode,
        log_level=_get("LOG_LEVEL", "INFO"),
        initial_paper_capital=float(_get("INITIAL_PAPER_CAPITAL", "100000")),
    )
