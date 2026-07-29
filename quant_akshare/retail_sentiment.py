from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
import re
from typing import Iterable

import requests

from .realtime import SpotQuote, WatchItem


GUBA_URL = "https://guba.eastmoney.com/list,{code}.html"

NEWBIE_KEYWORDS = {
    "身份自述": ["小白", "新手", "新人", "刚入", "第一次", "菜鸟", "萌新", "宝妈", "学生党"],
    "知识求助": ["不懂", "请教", "大佬", "请问", "求助", "怎么买", "在哪看", "什么意思"],
    "决策依赖": ["该不该", "要不要", "能不能", "可以吗", "靠谱吗", "还能上车吗", "还会涨吗", "还会跌吗"],
    "情绪恐慌": ["好慌", "救命", "完了", "哭了", "怕了", "心态崩", "亏死", "割肉", "后悔", "麻了"],
    "跟风行为": ["跟着买", "别人推荐", "博主说", "听说", "朋友说", "群里说", "都在买"],
    "过度乐观": ["冲", "梭哈", "稳赚", "必涨", "躺赚", "满仓干", "起飞", "暴富"],
    "短期思维": ["明天", "今天", "后天", "尾盘", "开盘", "涨停", "跌停"],
}

NEWBIE_WEIGHTS = {
    "身份自述": 8,
    "知识求助": 6,
    "决策依赖": 7,
    "情绪恐慌": 5,
    "跟风行为": 6,
    "过度乐观": 4,
    "短期思维": 3,
}

PRO_KEYWORDS = [
    "PE",
    "PB",
    "ROE",
    "估值",
    "基本面",
    "技术面",
    "MACD",
    "KDJ",
    "溢价率",
    "折价",
    "仓位",
    "轮动",
    "资产配置",
    "风险自担",
    "不构成建议",
]

BUY_KEYWORDS = [
    "上车",
    "冲",
    "梭哈",
    "满仓",
    "抄底",
    "加仓",
    "买入",
    "买了",
    "入手",
    "追",
    "建仓",
    "补仓",
    "还能买吗",
    "还能上车吗",
    "想买",
    "心动",
    "后悔没买",
]

SELL_KEYWORDS = [
    "割肉",
    "止损",
    "清仓",
    "减仓",
    "出货",
    "卖了",
    "跑了",
    "离场",
    "下车",
    "赎回",
    "要不要割",
    "要不要走",
    "想卖",
    "亏了",
    "亏麻了",
    "深套",
    "被套",
    "跌麻了",
    "血亏",
]

SPAM_KEYWORDS = ["金条", "签到", "打卡", "广告", "开户", "老师带", "群"]


@dataclass(frozen=True)
class AnalyzedPost:
    title: str
    score: float
    intent: str
    sentiment: float
    signals: tuple[str, ...]


def fetch_retail_sentiment(items: Iterable[WatchItem], quotes: dict[str, SpotQuote]) -> dict:
    results = []
    seen: set[str] = set()
    for item in items:
        quote = quotes.get(item.symbol)
        name = quote.name if quote else item.symbol
        key = item.symbol
        if key in seen:
            continue
        seen.add(key)
        results.append(_fetch_symbol_sentiment(item.symbol, name))

    valid = [row for row in results if row.get("postCount", 0) > 0]
    if not valid:
        return {
            "status": "empty",
            "source": "东方财富股吧",
            "updatedAt": datetime.now().strftime("%H:%M:%S"),
            "overall": _empty_summary("暂无股吧数据"),
            "items": results,
        }

    total_posts = sum(row["postCount"] for row in valid)
    weighted_index = sum(row["index"] * row["postCount"] for row in valid) / total_posts
    weighted_buy = sum(row["buyIndex"] * row["postCount"] for row in valid) / total_posts
    weighted_sell = sum(row["sellIndex"] * row["postCount"] for row in valid) / total_posts
    overall = _summary(weighted_index, total_posts, weighted_buy, weighted_sell)
    return {
        "status": "ok",
        "source": "东方财富股吧",
        "updatedAt": datetime.now().strftime("%H:%M:%S"),
        "overall": overall,
        "items": results,
    }


def _fetch_symbol_sentiment(symbol: str, name: str) -> dict:
    board_code = board_code_for(symbol, name)
    try:
        titles = parse_guba_titles(fetch_guba_html(board_code))
    except requests.RequestException as exc:
        return _symbol_empty(symbol, name, board_code, f"股吧读取失败: {exc.__class__.__name__}")
    analyzed = [analyze_title(title) for title in titles[:80]]
    return compute_symbol_sentiment(symbol, name, board_code, analyzed)


def board_code_for(symbol: str, name: str = "") -> str:
    if _is_fund(symbol, name):
        return f"of{symbol}"
    return symbol


def fetch_guba_html(board_code: str, timeout: float = 8.0) -> str:
    session = requests.Session()
    session.trust_env = False
    response = session.get(
        GUBA_URL.format(code=board_code),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://guba.eastmoney.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def parse_guba_titles(text: str) -> list[str]:
    titles = re.findall(r'<a[^>]+href="(?:https://guba\.eastmoney\.com)?/news,[^"]+"[^>]+title="([^"]+)"', text)
    if not titles:
        titles = re.findall(r'title="([^"]+)"[^>]+href="(?:https://guba\.eastmoney\.com)?/news,', text)
    cleaned: list[str] = []
    seen: set[str] = set()
    for title in titles:
        value = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if not value or value == "点击开始搜索" or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


def analyze_title(title: str) -> AnalyzedPost:
    if any(word in title for word in SPAM_KEYWORDS):
        return AnalyzedPost(title=title, score=0, intent="neutral", sentiment=0, signals=("垃圾/活动帖",))

    signals = []
    raw_score = 0.0
    for group, words in NEWBIE_KEYWORDS.items():
        hits = [word for word in words if word.lower() in title.lower()]
        if hits:
            raw_score += NEWBIE_WEIGHTS[group]
            signals.append(f"{group}:{','.join(hits[:2])}")

    if len(title) <= 12 and any(word in title for word in ["涨", "跌", "买", "卖", "冲"]):
        raw_score += 3
        signals.append("短标题情绪化")
    if title.endswith(("吗", "呢", "？", "?")):
        raw_score += 2
        signals.append("问句求决策")

    pro_hits = [word for word in PRO_KEYWORDS if word.lower() in title.lower()]
    raw_score -= len(pro_hits) * 3.5
    score = max(0, min(100, raw_score * 4 + 10))

    buy_hits = [word for word in BUY_KEYWORDS if word.lower() in title.lower()]
    sell_hits = [word for word in SELL_KEYWORDS if word.lower() in title.lower()]
    if len(buy_hits) > len(sell_hits):
        intent = "buy"
    elif len(sell_hits) > len(buy_hits):
        intent = "sell"
    else:
        intent = "neutral"
    sentiment = 0.0
    if buy_hits:
        sentiment += min(1.0, len(buy_hits) / 3)
    if sell_hits:
        sentiment -= min(1.0, len(sell_hits) / 3)
    return AnalyzedPost(title=title, score=round(score, 1), intent=intent, sentiment=sentiment, signals=tuple(signals[:3]))


def compute_symbol_sentiment(symbol: str, name: str, board_code: str, posts: list[AnalyzedPost]) -> dict:
    valid = [post for post in posts if "垃圾/活动帖" not in post.signals]
    if not valid:
        return _symbol_empty(symbol, name, board_code, "暂无有效帖子")
    newbie = [post for post in valid if post.score >= 20]
    pure_newbie = [post for post in valid if post.score >= 50]
    buy_posts = [post for post in newbie if post.intent == "buy"]
    sell_posts = [post for post in newbie if post.intent == "sell"]

    newbie_ratio = len(newbie) / len(valid) * 100
    avg_score = sum(post.score for post in newbie) / max(1, len(newbie))
    avg_extreme = sum(abs(post.sentiment) for post in newbie) / max(1, len(newbie)) * 100
    purity = len(pure_newbie) / max(1, len(newbie)) * 100
    index = min(100, newbie_ratio * 0.40 + avg_score * 0.25 + avg_extreme * 0.20 + purity * 0.15)
    buy_index = min(100, len(buy_posts) / max(1, len(newbie)) * 70 + sum(abs(p.sentiment) for p in buy_posts) / max(1, len(buy_posts)) * 30)
    sell_index = min(100, len(sell_posts) / max(1, len(newbie)) * 70 + sum(abs(p.sentiment) for p in sell_posts) / max(1, len(sell_posts)) * 30)
    top_posts = sorted(newbie, key=lambda post: post.score, reverse=True)[:3]
    return {
        "symbol": symbol,
        "name": name,
        "boardCode": board_code,
        "index": round(index, 1),
        "buyIndex": round(buy_index, 1),
        "sellIndex": round(sell_index, 1),
        "postCount": len(valid),
        "newbiePosts": len(newbie),
        "newbieRatio": round(newbie_ratio, 1),
        "signal": interpret_index(index),
        "level": level_for(index),
        "topPosts": [
            {"title": post.title[:48], "score": post.score, "intent": post.intent, "signals": list(post.signals)}
            for post in top_posts
        ],
        "error": "",
    }


def interpret_index(index: float) -> str:
    if index >= 75:
        return "极度狂热，反向高危"
    if index >= 60:
        return "高度警惕，少追高"
    if index >= 40:
        return "开始升温，谨慎加仓"
    if index >= 20:
        return "正常区间，跟随盘面"
    return "极度冷清，等待承接"


def level_for(index: float) -> str:
    if index >= 75:
        return "danger"
    if index >= 60:
        return "warn"
    if index >= 40:
        return "neutral"
    if index >= 20:
        return "good"
    return "cool"


def _summary(index: float, post_count: int, buy_index: float, sell_index: float) -> dict:
    return {
        "index": round(index, 1),
        "buyIndex": round(buy_index, 1),
        "sellIndex": round(sell_index, 1),
        "postCount": post_count,
        "signal": interpret_index(index),
        "level": level_for(index),
    }


def _empty_summary(signal: str) -> dict:
    return {"index": 0, "buyIndex": 0, "sellIndex": 0, "postCount": 0, "signal": signal, "level": "neutral"}


def _symbol_empty(symbol: str, name: str, board_code: str, error: str) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "boardCode": board_code,
        "index": 0,
        "buyIndex": 0,
        "sellIndex": 0,
        "postCount": 0,
        "newbiePosts": 0,
        "newbieRatio": 0,
        "signal": "暂无数据",
        "level": "neutral",
        "topPosts": [],
        "error": error,
    }


def _is_fund(symbol: str, name: str = "") -> bool:
    return "ETF" in name or "基金" in name or symbol.startswith(("15", "16", "18", "50", "51", "56", "58"))
