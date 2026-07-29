from __future__ import annotations

import math

import pandas as pd

from .models import Performance
from .strategy import moving_average_signal


def run_ma_backtest(
    prices: pd.DataFrame,
    short_window: int = 20,
    long_window: int = 60,
    initial_cash: float = 100_000.0,
    fee_rate: float = 0.0003,
    slippage_rate: float = 0.0005,
) -> tuple[pd.DataFrame, pd.DataFrame, Performance]:
    signals = moving_average_signal(prices, short_window, long_window)
    if signals.empty:
        raise ValueError("No prices available for backtest.")

    df = signals.copy()
    df["market_return"] = df["close"].pct_change().fillna(0.0)
    df["position"] = df["target_position"].shift(1).fillna(0).astype(int)
    df["trade"] = df["position"].diff().fillna(df["position"]).abs()
    df["cost"] = df["trade"] * (fee_rate + slippage_rate)
    df["strategy_return"] = df["position"] * df["market_return"] - df["cost"]
    df["equity"] = initial_cash * (1 + df["strategy_return"]).cumprod()
    df["benchmark_equity"] = initial_cash * (1 + df["market_return"]).cumprod()
    df["drawdown"] = df["equity"] / df["equity"].cummax() - 1

    trades = df.loc[df["trade"] > 0, ["date", "close", "position", "signal"]].copy()
    trades["action"] = trades["position"].map({1: "BUY", 0: "SELL"})
    trades = trades[["date", "action", "close", "signal"]]

    performance = calculate_performance(df, len(trades), initial_cash)
    return df, trades, performance


def calculate_performance(
    daily: pd.DataFrame,
    trade_count: int,
    initial_cash: float,
) -> Performance:
    final_equity = float(daily["equity"].iloc[-1])
    total_return = final_equity / initial_cash - 1
    days = max((daily["date"].iloc[-1] - daily["date"].iloc[0]).days, 1)
    annual_return = math.pow(1 + total_return, 365 / days) - 1
    max_drawdown = float(daily["drawdown"].min())
    return Performance(
        total_return=total_return,
        annual_return=annual_return,
        max_drawdown=max_drawdown,
        trade_count=trade_count,
        final_equity=final_equity,
    )

