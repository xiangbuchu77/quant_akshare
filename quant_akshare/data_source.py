from __future__ import annotations

import pandas as pd


REQUIRED_COLUMNS = ["date", "open", "close", "high", "low", "volume"]


def normalize_akshare_hist(raw: pd.DataFrame) -> pd.DataFrame:
    column_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "振幅": "amplitude",
        "涨跌幅": "pct_change",
        "涨跌额": "change",
        "换手率": "turnover",
    }
    df = raw.rename(columns=column_map).copy()
    missing = [name for name in REQUIRED_COLUMNS if name not in df.columns]
    if missing:
        raise ValueError(f"AKShare response missing required columns: {missing}")

    df = df[[col for col in column_map.values() if col in df.columns]]
    df["date"] = pd.to_datetime(df["date"])
    numeric_cols = [col for col in df.columns if col != "date"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date", "open", "close", "high", "low"])
    return df.sort_values("date").drop_duplicates("date").reset_index(drop=True)


def fetch_stock_daily(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "qfq",
) -> pd.DataFrame:
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError(
            "AKShare is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    raw = ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start,
        end_date=end,
        adjust=adjust,
    )
    return normalize_akshare_hist(raw)

