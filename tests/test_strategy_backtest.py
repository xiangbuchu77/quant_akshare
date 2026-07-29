from __future__ import annotations

import unittest

import pandas as pd

from quant_akshare.backtest import run_ma_backtest
from quant_akshare.strategy import latest_signal, moving_average_signal


def make_prices() -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=90, freq="D")
    closes = [10 + i * 0.03 for i in range(45)] + [11.35 - i * 0.04 for i in range(45)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "close": closes,
            "high": [price * 1.01 for price in closes],
            "low": [price * 0.99 for price in closes],
            "volume": [1_000_000] * len(closes),
        }
    )


class StrategyBacktestTest(unittest.TestCase):
    def test_moving_average_signal_has_expected_columns(self) -> None:
        signals = moving_average_signal(make_prices(), short_window=5, long_window=20)

        self.assertIn("ma_short", signals.columns)
        self.assertIn("ma_long", signals.columns)
        self.assertIn("target_position", signals.columns)
        self.assertIn("signal", signals.columns)
        self.assertEqual(len(signals), 90)

    def test_latest_signal_returns_known_state(self) -> None:
        signals = moving_average_signal(make_prices(), short_window=5, long_window=20)

        self.assertIn(latest_signal(signals), {"BUY", "SELL", "HOLD", "CASH"})

    def test_backtest_outputs_daily_frame_trades_and_performance(self) -> None:
        daily, trades, performance = run_ma_backtest(
            make_prices(),
            short_window=5,
            long_window=20,
            initial_cash=100_000,
        )

        self.assertEqual(len(daily), 90)
        self.assertIn("equity", daily.columns)
        self.assertIn("drawdown", daily.columns)
        self.assertGreater(performance.final_equity, 0)
        self.assertEqual(performance.trade_count, len(trades))


if __name__ == "__main__":
    unittest.main()

