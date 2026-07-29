from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_prices(db_path: Path, symbol: str, prices: pd.DataFrame) -> None:
    ensure_parent(db_path)
    df = prices.copy()
    df["symbol"] = symbol
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    db_columns = [
        "symbol",
        "date",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "amount",
        "amplitude",
        "pct_change",
        "change",
        "turnover",
    ]
    for col in db_columns:
        if col not in df.columns:
            df[col] = None
    df = df[db_columns]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_daily (
                symbol TEXT NOT NULL,
                date TEXT NOT NULL,
                open REAL NOT NULL,
                close REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                volume REAL,
                amount REAL,
                amplitude REAL,
                pct_change REAL,
                change REAL,
                turnover REAL,
                PRIMARY KEY (symbol, date)
            )
            """
        )
        df.to_sql("_stock_daily_stage", conn, if_exists="replace", index=False)
        conn.execute(
            """
            INSERT OR REPLACE INTO stock_daily
            SELECT symbol, date, open, close, high, low, volume, amount,
                   amplitude, pct_change, change, turnover
            FROM _stock_daily_stage
            """
        )
        conn.execute("DROP TABLE _stock_daily_stage")


def read_prices(db_path: Path, symbol: str, start: str, end: str) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()

    start_date = pd.to_datetime(start).strftime("%Y-%m-%d")
    end_date = pd.to_datetime(end).strftime("%Y-%m-%d")
    with sqlite3.connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT date, open, close, high, low, volume, amount, amplitude,
                   pct_change, change, turnover
            FROM stock_daily
            WHERE symbol = ? AND date >= ? AND date <= ?
            ORDER BY date
            """,
            conn,
            params=(symbol, start_date, end_date),
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    return df
