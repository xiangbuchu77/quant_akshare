from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from quant_akshare.portfolio_book import PortfolioBookStore


class PortfolioBookStoreTest(unittest.TestCase):
    def test_migrates_legacy_files_and_keeps_mirrors_in_sync(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "qclaw_portfolio.json"
            trade_path = root / "qclaw_trades.jsonl"
            snapshot_path = root / "daily_position_snapshots.json"
            book_path = root / "portfolio_book.json"
            legacy_state = {
                "holdings": [{"symbol": "588000", "cost": 2.011, "shares": 30600}],
                "watchlist": ["159869"],
                "order": ["588000", "159869"],
                "line_overrides": {},
            }
            state_path.write_text(json.dumps(legacy_state), encoding="utf-8")
            trade_path.write_text(
                json.dumps(
                    {
                        "id": "trade-1",
                        "time": "2026-07-28T09:30:00",
                        "symbol": "588000",
                        "side": "buy",
                        "price": 1.962,
                        "shares": 6200,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            snapshot_path.write_text(
                json.dumps({"2026-07-28": {"positions": [{"symbol": "588000", "shares": 24400}]}}),
                encoding="utf-8",
            )
            store = PortfolioBookStore(book_path, state_path, trade_path, snapshot_path)

            migrated = store.load(legacy_state)
            initial_revision = migrated["revision"]

            def mutate(book: dict) -> None:
                book["state"]["holdings"][0]["shares"] = 36800
                book["trades"].append({"id": "trade-2", "symbol": "588000"})

            updated = store.update(legacy_state, mutate)

            self.assertTrue(book_path.exists())
            self.assertGreater(updated["revision"], initial_revision)
            self.assertEqual(json.loads(state_path.read_text())["holdings"][0]["shares"], 36800)
            self.assertEqual(len(trade_path.read_text().splitlines()), 2)
            self.assertEqual(json.loads(book_path.read_text())["state"], json.loads(state_path.read_text()))
