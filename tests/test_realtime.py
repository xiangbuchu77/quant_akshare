from __future__ import annotations

from datetime import datetime
import unittest

from quant_akshare.realtime import (
    build_watch_items,
    evaluate_alerts,
    market_prefix,
    parse_spot_quote,
    parse_symbol_map,
    tencent_prefix,
    to_secid,
)


class RealtimeTest(unittest.TestCase):
    def test_market_prefix_and_secid(self) -> None:
        self.assertEqual(market_prefix("600030"), "1")
        self.assertEqual(market_prefix("588000"), "1")
        self.assertEqual(market_prefix("002463"), "0")
        self.assertEqual(to_secid("600030"), "1.600030")
        self.assertEqual(to_secid("002463"), "0.002463")
        self.assertEqual(tencent_prefix("600030"), "sh")
        self.assertEqual(tencent_prefix("002463"), "sz")

    def test_parse_symbol_map(self) -> None:
        self.assertEqual(
            parse_symbol_map("002463=129.156,600030=26"),
            {"002463": 129.156, "600030": 26.0},
        )

    def test_evaluate_alerts_for_cost_and_stop(self) -> None:
        quote = parse_spot_quote(
            {
                "f12": "002463",
                "f14": "沪电股份",
                "f2": 123.5,
                "f3": -2.0,
                "f4": -2.5,
                "f5": 100,
                "f6": 100000000,
                "f8": 2.5,
                "f9": 48,
                "f15": 130,
                "f16": 123,
                "f17": 128,
                "f18": 126,
                "f23": 15,
            },
            fetched_at=datetime(2026, 6, 12, 10, 0, 0),
        )
        item = build_watch_items(
            ["002463"],
            costs={"002463": 129.156},
            shares={"002463": 400},
            stop_lines={"002463": 124},
        )[0]

        alerts = evaluate_alerts(quote, item)

        self.assertTrue(any("成本浮亏" in alert for alert in alerts))
        self.assertTrue(any("跌破风控线" in alert for alert in alerts))
        self.assertEqual(item.shares, 400)


if __name__ == "__main__":
    unittest.main()
