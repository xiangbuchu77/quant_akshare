from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from quant_akshare.qclaw_service import (
    QClawState,
    _portfolio_metrics,
    auto_risk_line,
    clear_watchlist,
    apply_trade,
    append_trade_log,
    revoke_trade,
    merge_holding_trades,
    merge_holdings,
    merge_watchlist,
    reorder_symbols,
    replace_holdings_snapshot,
    remove_holdings,
    remove_watchlist,
    update_risk_lines,
    state_to_items,
    write_state,
)
from quant_akshare.realtime import SpotQuote


class QClawServiceTest(unittest.TestCase):
    def test_holdings_and_watchlist_convert_to_items(self) -> None:
        state = QClawState(holdings=[], watchlist=[])
        state = merge_holdings(
            state,
            [{"symbol": "002463", "cost": 147.635, "shares": 200, "stop": 132, "target": 140}],
            persist=False,
        )
        state = merge_watchlist(state, ["300750", "002463"], persist=False)

        self.assertEqual(len(state.holdings), 1)
        self.assertEqual(state.watchlist, ["300750"])

        items = state_to_items(state)
        self.assertEqual([item.symbol for item in items], ["002463", "300750"])
        self.assertEqual(items[0].cost, 147.635)
        self.assertEqual(items[0].shares, 200.0)
        self.assertIsNone(items[1].cost)
        self.assertIsNone(items[1].shares)

    def test_remove_holdings_and_watchlist(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "002463", "cost": 147.635, "shares": 200}],
            watchlist=["300750", "000725"],
        )

        state = remove_watchlist(state, ["300750"], persist=False)
        self.assertEqual(state.watchlist, ["000725"])

        state = remove_holdings(state, ["002463"], persist=False)
        self.assertEqual(state.holdings, [])

        state = clear_watchlist(state, persist=False)
        self.assertEqual(state.watchlist, [])

    def test_apply_trade_updates_holdings(self) -> None:
        state = QClawState(holdings=[], watchlist=["000725"], order=["000725"], line_overrides={})
        state = apply_trade(state, symbol="000725", side="buy", price=6.92, shares=900, persist=False)

        self.assertEqual(state.watchlist, [])
        self.assertEqual(state.holdings[0]["symbol"], "000725")
        self.assertEqual(state.holdings[0]["shares"], 900)
        self.assertEqual(state.holdings[0]["cost"], 6.92)

        state = apply_trade(state, symbol="000725", side="sell", price=7.0, shares=300, persist=False)
        self.assertEqual(state.holdings[0]["shares"], 600)

    def test_sell_all_moves_holding_to_watchlist(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "600226", "cost": 11.48, "shares": 100}],
            watchlist=[],
            order=["600226"],
            line_overrides={},
        )

        state = apply_trade(state, symbol="600226", side="sell", price=11.2, shares=100, persist=False)

        self.assertEqual(state.holdings, [])
        self.assertEqual(state.watchlist, ["600226"])
        self.assertEqual(state.order, ["600226"])

    def test_apply_trade_adds_to_existing_holding(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "000725", "cost": 6.92, "shares": 900}],
            watchlist=[],
            order=["000725"],
            line_overrides={},
        )

        state = apply_trade(state, symbol="000725", side="买入", price=6.99, shares=1200, persist=False)

        self.assertEqual(state.holdings[0]["shares"], 2100)
        self.assertAlmostEqual(state.holdings[0]["cost"], 6.96, places=2)

    def test_apply_trade_accepts_etf_three_decimal_price(self) -> None:
        state = QClawState(holdings=[], watchlist=["159869"], order=["159869"], line_overrides={})

        state = apply_trade(state, symbol="159869", side="buy", price=1.088, shares=1000, persist=False)

        self.assertEqual(state.watchlist, [])
        self.assertEqual(state.holdings[0]["symbol"], "159869")
        self.assertEqual(state.holdings[0]["shares"], 1000)
        self.assertAlmostEqual(state.holdings[0]["cost"], 1.088, places=3)

    def test_revoke_trade_restores_local_holding(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state = QClawState(
                holdings=[{"symbol": "159869", "cost": 1.1, "shares": 1100}],
                watchlist=[],
                order=["159869"],
                line_overrides={},
            )
            with patch("quant_akshare.qclaw_service.TRADE_LOG_PATH", root / "trades.jsonl"), patch(
                "quant_akshare.qclaw_service.STATE_PATH", root / "portfolio.json"
            ):
                write_state(state)
                record = append_trade_log(
                    symbol="159869", side="buy", price=1.2, shares=100,
                    cost_basis=1.09, trade_time="2026-07-14T10:00:00",
                )
                restored = revoke_trade(state, record["id"])
            self.assertEqual(restored.holdings[0]["shares"], 1000)
            self.assertAlmostEqual(restored.holdings[0]["cost"], 1.09, places=6)

    def test_portfolio_metrics_use_opening_position_and_cash_flows(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "588000", "cost": 10.0, "shares": 900}],
            watchlist=[],
            order=["588000"],
            line_overrides={},
        )
        quote = SpotQuote(
            symbol="588000",
            name="科创50ETF华夏",
            price=10.5,
            pct_change=5.0,
            change=0.5,
            open=10.1,
            high=10.6,
            low=10.0,
            prev_close=10.0,
            volume=100,
            amount=1_000_000,
            turnover=1.2,
            pe=0,
            pb=0,
            fetched_at=datetime(2026, 7, 28, 10, 0, 0),
        )
        trades = [
            {
                "id": "sell-1",
                "time": "2026-07-28T09:35:00",
                "symbol": "588000",
                "side": "sell",
                "price": 11.0,
                "shares": 100,
                "cost_basis": 10.0,
            }
        ]

        metrics = _portfolio_metrics(
            state,
            [{"symbol": "588000", "shares": 1000}],
            trades,
            {"588000": quote},
        )

        self.assertEqual(metrics["dailyPnl"], 550.0)
        self.assertEqual(metrics["realizedPnl"], 100.0)
        self.assertEqual(metrics["unrealizedPnl"], 450.0)
        self.assertTrue(metrics["complete"])

    def test_account_snapshot_is_authoritative_for_daily_pnl(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "588000", "cost": 10.0, "shares": 900}],
            watchlist=[],
            order=["588000"],
            line_overrides={},
        )
        quote = SpotQuote(
            symbol="588000",
            name="科创50ETF华夏",
            price=10.5,
            pct_change=5.0,
            change=0.5,
            open=10.1,
            high=10.6,
            low=10.0,
            prev_close=10.0,
            volume=100,
            amount=1_000_000,
            turnover=1.2,
            pe=0,
            pb=0,
            fetched_at=datetime(2026, 7, 28, 15, 0, 0),
        )
        trades = [
            {
                "id": "sell-1",
                "time": "2026-07-28T09:35:00",
                "symbol": "588000",
                "side": "sell",
                "price": 11.0,
                "shares": 100,
                "cost_basis": 10.0,
            }
        ]
        account = {
            "capturedAt": "2026-07-28T15:00:00",
            "totalAssets": 80_531.62,
            "marketValue": 9_000.0,
            "cashComponent": 71_531.62,
            "openingAssets": 80_205.62,
            "netTransfer": 0.0,
            "reportedDailyPnl": 326.0,
        }

        metrics = _portfolio_metrics(
            state,
            [{"symbol": "588000", "shares": 1000}],
            trades,
            {"588000": quote},
            account,
        )

        self.assertEqual(metrics["dailyPnl"], 326.0)
        self.assertEqual(metrics["positionDailyPnl"], 550.0)
        self.assertEqual(metrics["dailyPnlSource"], "account")
        self.assertAlmostEqual(metrics["dailyPnlRate"], 0.00406455, places=8)
        self.assertEqual(metrics["accountTotalAssets"], 80_531.62)

    def test_trade_mode_import_adds_instead_of_overwriting(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "000725", "cost": 6.92, "shares": 900}],
            watchlist=[],
            order=["000725"],
            line_overrides={},
        )

        state = merge_holding_trades(
            state,
            [{"symbol": "000725", "side": "buy", "price": 6.99, "shares": 1200}],
            persist=False,
        )

        self.assertEqual(state.holdings[0]["shares"], 2100)
        self.assertAlmostEqual(state.holdings[0]["cost"], 6.96, places=2)

    def test_replace_snapshot_moves_old_holding_to_watchlist_and_keeps_negative_cost(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "588000", "cost": 2.011, "shares": 30600}],
            watchlist=["159869"],
            order=["588000", "159869"],
            line_overrides={},
        )

        state = replace_holdings_snapshot(
            state,
            [
                {"symbol": "000021", "cost": 41.037, "shares": 300},
                {"symbol": "002043", "cost": -5.036, "shares": 100},
                {"symbol": "000938", "cost": 39.984, "shares": 500},
            ],
            persist=False,
        )

        self.assertEqual([item["symbol"] for item in state.holdings], ["000021", "002043", "000938"])
        self.assertEqual(state.holdings[1]["cost"], -5.036)
        self.assertEqual(state.watchlist, ["159869", "588000"])
        self.assertEqual(state.order[:3], ["000021", "002043", "000938"])

    def test_manual_lines_and_order_override(self) -> None:
        state = QClawState(
            holdings=[{"symbol": "002463", "cost": 147.635, "shares": 200}],
            watchlist=["300750"],
            order=["002463", "300750"],
            line_overrides={},
        )
        state = update_risk_lines(
            state,
            [{"symbol": "002463", "stop": 135, "target": 148}],
            persist=False,
        )
        state = reorder_symbols(state, ["300750", "002463"], persist=False)
        items = state_to_items(state, auto_lines={"002463": {"stop": 130, "target": 145}})

        self.assertEqual([item.symbol for item in items], ["300750", "002463"])
        self.assertEqual(items[1].rules[0].below, 135)
        self.assertEqual(items[1].rules[1].above, 148)

    def test_auto_risk_line_uses_cost_and_volatility(self) -> None:
        quote = SpotQuote(
            symbol="002463",
            name="沪电股份",
            price=140.0,
            pct_change=2.5,
            change=3.4,
            open=136.5,
            high=143.0,
            low=136.0,
            prev_close=136.6,
            volume=0,
            amount=1_000_000,
            turnover=1.2,
            pe=50,
            pb=10,
            fetched_at=datetime(2026, 6, 24, 10, 0, 0),
        )

        holding_lines = auto_risk_line(quote, cost=130.0, shares=200)
        watch_lines = auto_risk_line(quote)

        self.assertGreater(holding_lines["target"], quote.price)
        self.assertLess(holding_lines["stop"], quote.price)
        self.assertGreaterEqual(holding_lines["stop"], 126.1)
        self.assertGreater(watch_lines["target"], quote.price)
        self.assertLess(watch_lines["stop"], quote.price)


if __name__ == "__main__":
    unittest.main()
