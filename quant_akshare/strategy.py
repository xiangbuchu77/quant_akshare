from __future__ import annotations

import pandas as pd


def moving_average_signal(
    prices: pd.DataFrame,
    short_window: int = 20,
    long_window: int = 60,
) -> pd.DataFrame:
    if short_window <= 0 or long_window <= 0:
        raise ValueError("Moving average windows must be positive.")
    if short_window >= long_window:
        raise ValueError("short_window must be smaller than long_window.")
    if "close" not in prices.columns:
        raise ValueError("prices must include a close column.")

    df = prices.sort_values("date").copy()
    df["ma_short"] = df["close"].rolling(short_window).mean()
    df["ma_long"] = df["close"].rolling(long_window).mean()
    df["target_position"] = (df["ma_short"] > df["ma_long"]).astype(int)
    df.loc[df["ma_long"].isna(), "target_position"] = 0
    df["signal"] = df["target_position"].diff().fillna(df["target_position"])
    return df


def latest_signal(signal_frame: pd.DataFrame) -> str:
    if signal_frame.empty:
        return "NO_DATA"
    row = signal_frame.iloc[-1]
    if row["signal"] > 0:
        return "BUY"
    if row["signal"] < 0:
        return "SELL"
    if row["target_position"] > 0:
        return "HOLD"
    return "CASH"

