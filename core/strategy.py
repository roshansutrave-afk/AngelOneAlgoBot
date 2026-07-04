"""
core/strategy.py
Strategy interface. Every concrete strategy takes the latest
indicator-enriched DataFrame and returns a Signal — nothing more.
Strategies never call the broker directly; that's execution.py's job.
This separation is what lets a rule-based strategy be swapped for an
RL policy later without touching the rest of the pipeline.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
import pandas as pd


class Action(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


@dataclass(frozen=True)
class Signal:
    action: Action
    confidence: float  # 0.0-1.0, used by risk_manager for position sizing
    reason: str


class Strategy(ABC):
    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """df is the indicator-enriched OHLCV frame, most recent row last."""
        raise NotImplementedError
