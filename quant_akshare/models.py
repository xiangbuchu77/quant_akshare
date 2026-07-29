from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    start: str
    end: str
    short_window: int = 20
    long_window: int = 60
    initial_cash: float = 100_000.0
    fee_rate: float = 0.0003
    slippage_rate: float = 0.0005


@dataclass(frozen=True)
class Performance:
    total_return: float
    annual_return: float
    max_drawdown: float
    trade_count: int
    final_equity: float

