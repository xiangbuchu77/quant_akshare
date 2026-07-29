from __future__ import annotations

import unittest

from quant_akshare.ai_server import build_chat_prompt, build_prompt
from datetime import datetime

from quant_akshare.dashboard import _fetch_sector_flows, _html_document, _price_fmt, build_position_view
from quant_akshare.realtime import SpotQuote, WatchItem
from quant_akshare.retail_sentiment import analyze_title, board_code_for, compute_symbol_sentiment, parse_guba_titles


class DashboardHtmlTest(unittest.TestCase):
    def test_dashboard_contains_fund_flow_and_trade_controls(self) -> None:
        html = _html_document(
            title="测试看板",
            views=[],
            payload={
                "refreshSeconds": 5,
                "market": [{"symbol": "sh000001", "name": "上证指数"}],
                "sectorFlows": [
                    {"code": "BK1036", "name": "半导体", "pct": 1.2, "mainNet": 100000000, "mainNetRatio": 2.1},
                    {"code": "BK1216", "name": "医药生物", "pct": -0.8, "mainNet": -90000000, "mainNetRatio": -2.0},
                ],
                "retailSentiment": {
                    "source": "东方财富股吧",
                    "updatedAt": "10:00:00",
                    "overall": {"index": 45.0, "buyIndex": 30.0, "sellIndex": 10.0, "postCount": 20, "signal": "开始升温", "level": "neutral"},
                    "items": [
                        {"symbol": "159869", "name": "游戏ETF华夏", "boardCode": "of159869", "index": 45.0, "buyIndex": 30.0, "sellIndex": 10.0, "postCount": 20, "newbieRatio": 25.0, "signal": "开始升温", "level": "neutral", "topPosts": []}
                    ],
                },
                "tradeDate": "2026-07-09",
                "todayTrades": [
                    {"time": "2026-07-09T09:30:00", "symbol": "002463", "side": "buy", "price": 1.088, "shares": 1000},
                    {"time": "2026-07-09T10:00:00", "symbol": "002463", "side": "sell", "price": 1.100, "shares": 500},
                ],
                "positions": [
                    {
                        "symbol": "002463",
                        "cost": 147.635,
                        "shares": 200,
                        "stop": 132,
                        "target": 140,
                        "prefix": "sz",
                        "secid": "0.002463",
                        "kind": "holding",
                    },
                    {
                        "symbol": "300750",
                        "cost": None,
                        "shares": None,
                        "stop": None,
                        "target": None,
                        "prefix": "sz",
                        "secid": "0.300750",
                        "kind": "watch",
                    }
                ],
            },
        )

        self.assertIn("主力净流入", html)
        self.assertIn("flowScore", html)
        self.assertIn("dynamicLines", html)
        self.assertIn("fflow/kline/get", html)
        self.assertIn("initialQuotes", html)
        self.assertIn("tradeForm", html)
        self.assertIn('id="tradePrice" type="number" step="0.001"', html)
        self.assertIn("AI_ANALYSIS_KEY", html)
        self.assertIn("自选观察", html)
        self.assertIn("DeepSeek 接入方式", html)
        self.assertIn("量能", html)
        self.assertIn("volumeEval", html)
        self.assertIn("refreshVolumeProfiles", html)
        self.assertIn("支撑位", html)
        self.assertIn("supportEval", html)
        self.assertIn("当日板块资金流向", html)
        self.assertIn("股吧散户情绪温度计", html)
        self.assertIn("renderRetailSentiment", html)
        self.assertIn("latestRetailSentiment", html)
        self.assertIn("parseSectorFlowPayload", html)
        self.assertIn("fetchSectorFlowPage", html)
        self.assertIn("refreshSectorFlows", html)
        self.assertIn("DASHBOARD.sectorFlows", html)
        self.assertIn("半导体", html)
        self.assertIn("po=${direction}", html)
        self.assertIn("持仓K线", html)
        self.assertIn("klineGrid", html)
        self.assertIn("drawKlineChart", html)
        self.assertIn("renderKlineCharts", html)
        self.assertIn("bindKlineInteraction", html)
        self.assertIn("drawKlineHover", html)
        self.assertIn("klineChartState", html)
        self.assertIn("priceNum(p.cost, q)", html)
        self.assertIn("priceNum(p.stop, q)", html)
        self.assertIn("priceNum(p.target, q)", html)
        self.assertIn("chart-title", html)
        self.assertIn("displayNameForSymbol(p.symbol)", html)
        self.assertIn("持仓分时线", html)
        self.assertIn("intradayGrid", html)
        self.assertIn("trends2/get", html)
        self.assertIn("parseIntradayPayload", html)
        self.assertIn("refreshIntradayProfiles", html)
        self.assertIn("intradayEvaluation", html)
        self.assertIn("requestDeepSeekAnalysis", html)
        self.assertIn("qclaw-deepseek", html)
        self.assertIn("analysis-cards", html)
        self.assertIn("renderAiAnalysis", html)
        self.assertIn("replaceSymbolsWithNames", html)
        self.assertIn("aiChatForm", html)
        self.assertIn("AI_CHAT_ENDPOINT", html)
        self.assertIn("requestAiChat", html)
        self.assertIn("submitAiChat", html)
        self.assertIn("今日收益 / 做T建议", html)
        self.assertIn("DASHBOARD_TRADE_DATE", html)
        self.assertIn("normalizeTodayTrades", html)
        self.assertIn("appendTodayTrade", html)
        self.assertIn("今日交易 ·", html)
        self.assertIn("renderTodayTrades", html)
        self.assertIn("tAdvice", html)
        self.assertIn("券商当日盈亏", html)
        self.assertIn("持仓估算盈亏", html)
        self.assertIn("accountSnapshotForm", html)
        self.assertIn("update_account_snapshot", html)
        self.assertIn("dailyAccountPnl", html)
        self.assertIn("dailyBaseline", html)
        self.assertIn("positionUnreconciled", html)
        self.assertIn("仅已核对", html)
        self.assertIn("未记成交", html)
        self.assertIn("QCLAW_SNAPSHOT_ENDPOINT", html)
        self.assertIn("syncDynamicSnapshot", html)
        self.assertIn("const previousSymbol = preferredSymbol || select.value", html)
        self.assertIn("select.value = previousSymbol", html)
        self.assertIn("账本已同步", html)
        self.assertIn("已实现盈亏（今日卖出）", html)
        self.assertIn("当前浮盈亏", html)
        self.assertIn("做T收益（成交配对）", html)
        self.assertIn("持仓和交易日志均未修改", html)
        self.assertIn("股票管理", html)
        self.assertIn("addStockForm", html)
        self.assertIn("removeWatchForm", html)
        self.assertIn("新增股票", html)
        self.assertIn("删除自选", html)
        self.assertIn('postQClawAction("apply_trade", { symbol, side: "buy", price: cost, shares })', html)
        self.assertIn("已按今日买入加入持仓和成交记录", html)
        self.assertIn("postQClawAction", html)
        self.assertIn("dashboardTradeDayIsCurrent", html)
        self.assertIn("refreshForNewTradeDay", html)
        self.assertIn("等待今日基准", html)
        self.assertIn("清空今日记录", html)
        self.assertIn("data-revoke-trade", html)
        self.assertIn("revokeTrade", html)
        self.assertNotIn("flowGrid", html)
        self.assertNotIn("flow-chip", html)
        self.assertNotIn("复盘总览", html)
        self.assertNotIn("复盘流程", html)
        self.assertNotIn("持仓分层", html)

    def test_deepseek_prompt_uses_short_term_strategy(self) -> None:
        prompt = build_prompt({"positions": [], "market": {}})

        self.assertIn("A股短线盯盘辅助分析员", prompt)
        self.assertIn("分时均价线", prompt)
        self.assertIn("资金与量能", prompt)
        self.assertIn("操作计划", prompt)
        self.assertIn("失效条件", prompt)
        self.assertIn("优先使用股票名称", prompt)
        self.assertIn("不要用股票编号", prompt)

    def test_deepseek_chat_prompt_is_short_and_snapshot_based(self) -> None:
        prompt = build_chat_prompt(
            {"positions": [{"quote": {"name": "游戏ETF华夏"}}]},
            "现在最需要盯哪只？",
            [{"role": "user", "content": "先看风险"}],
        )

        self.assertIn("A股短线盯盘助手", prompt)
        self.assertIn("最多4句话", prompt)
        self.assertIn("当前快照JSON", prompt)
        self.assertIn("现在最需要盯哪只", prompt)

    def test_price_format_keeps_meaningful_third_decimal(self) -> None:
        self.assertEqual(_price_fmt(1.097), "1.097")
        self.assertEqual(_price_fmt(6.01), "6.01")
        self.assertEqual(_price_fmt(2.21, "588000", "科创50ETF华夏"), "2.210")
        self.assertEqual(_price_fmt(6.01, "600996", "贵广网络"), "6.01")

    def test_negative_cost_keeps_profit_amount_without_invalid_profit_rate(self) -> None:
        quote = SpotQuote(
            symbol="002043",
            name="兔宝宝",
            price=12.84,
            pct_change=0,
            change=0,
            open=12.84,
            high=12.84,
            low=12.84,
            prev_close=12.84,
            volume=0,
            amount=0,
            turnover=0,
            pe=0,
            pb=0,
            fetched_at=datetime(2026, 7, 28, 15, 0),
        )
        view = build_position_view(quote, WatchItem(symbol="002043", cost=-5.036, shares=100))

        self.assertIsNone(view.pnl_pct)
        self.assertAlmostEqual(view.pnl_amount, 1787.6)
        self.assertNotEqual(view.advice_level, "danger")

    def test_retail_sentiment_uses_guba_titles(self) -> None:
        html = '<a href="/news,of159869,1.html" title="小白还能上车吗"></a>'
        self.assertEqual(parse_guba_titles(html), ["小白还能上车吗"])
        self.assertEqual(board_code_for("159869", "游戏ETF华夏"), "of159869")
        post = analyze_title("小白还能上车吗，想买一点")
        self.assertGreater(post.score, 20)
        self.assertEqual(post.intent, "buy")
        result = compute_symbol_sentiment("159869", "游戏ETF华夏", "of159869", [post])
        self.assertGreater(result["index"], 0)
        self.assertEqual(result["boardCode"], "of159869")

    def test_fetch_sector_flows_can_be_empty_without_crashing(self) -> None:
        flows = _fetch_sector_flows()

        self.assertIsInstance(flows, list)


if __name__ == "__main__":
    unittest.main()
