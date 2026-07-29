from __future__ import annotations

from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
import html
import json
from math import isfinite
from pathlib import Path
import re
import time
from typing import Iterable

import akshare as ak
import requests

from .realtime import SpotQuote, WatchItem


GUBA_URL = "https://guba.eastmoney.com/list,{code}.html"
HEAT_CACHE_PATH = Path(__file__).resolve().parents[1] / "data" / "retail_heat_cache.json"
HEAT_CACHE_TTL_SECONDS = 30 * 60
SOURCE_WEIGHTS = {
    "guba": 0.60,
    "eastmoney": 0.15,
    "xueqiu": 0.20,
    "weibo": 0.05,
}
SOURCE_LABELS = {
    "guba": "股吧语义",
    "eastmoney": "东财热度",
    "xueqiu": "雪球讨论",
    "weibo": "微博舆情",
}

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
    item_list = list(items)
    heat = fetch_cross_platform_heat()
    heat_profiles = dict(heat.get("profiles") or {})
    weibo_by_name = dict(heat.get("weiboByName") or {})
    targets: list[tuple[WatchItem, str]] = []
    seen: set[str] = set()
    for item in item_list:
        quote = quotes.get(item.symbol)
        name = quote.name if quote else item.symbol
        key = item.symbol
        if key in seen:
            continue
        seen.add(key)
        targets.append((item, name))

    guba_rows: list[dict] = []
    if targets:
        with ThreadPoolExecutor(max_workers=min(6, len(targets))) as pool:
            guba_rows = list(
                pool.map(
                    lambda target: _fetch_symbol_sentiment(target[0].symbol, target[1]),
                    targets,
                )
            )

    results = []
    for (item, name), guba_row in zip(targets, guba_rows):
        profile = dict(heat_profiles.get(item.symbol) or {})
        weibo = weibo_by_name.get(_normalize_name(name))
        if weibo:
            profile["weibo"] = dict(weibo)
        results.append(apply_source_weights(guba_row, profile))

    valid = [row for row in results if row.get("scoreCoverage", 0) > 0]
    if not valid:
        return {
            "status": "empty",
            "source": "股吧语义 + 东财热度 + 雪球讨论 + 微博舆情",
            "updatedAt": datetime.now().strftime("%H:%M:%S"),
            "overall": _empty_summary("暂无可用情绪数据"),
            "items": results,
            "sourceStatus": heat.get("sourceStatus") or {},
        }

    total_posts = sum(row["postCount"] for row in valid)
    total_weight = sum(max(1, row["postCount"]) for row in valid)
    weighted_index = sum(row["index"] * max(1, row["postCount"]) for row in valid) / total_weight
    weighted_buy = sum(row["buyIndex"] * max(1, row["postCount"]) for row in valid) / total_weight
    weighted_sell = sum(row["sellIndex"] * max(1, row["postCount"]) for row in valid) / total_weight
    overall = _summary(weighted_index, total_posts, weighted_buy, weighted_sell)
    overall["trackedCount"] = len(valid)
    overall["sourceCount"] = len(
        {
            source["source"]
            for row in valid
            for source in row.get("heatSources", [])
            if source.get("source")
        }
    )
    return {
        "status": "ok",
        "source": "股吧语义 + 东财热度 + 雪球讨论 + 微博舆情",
        "updatedAt": datetime.now().strftime("%H:%M:%S"),
        "overall": overall,
        "items": results,
        "sourceStatus": heat.get("sourceStatus") or {},
    }


def fetch_cross_platform_heat(
    cache_path: Path = HEAT_CACHE_PATH,
    ttl_seconds: int = HEAT_CACHE_TTL_SECONDS,
) -> dict:
    cached = _load_heat_cache(cache_path)
    fetched_at = _safe_float(cached.get("fetchedAt"))
    if fetched_at is not None and time.time() - fetched_at < ttl_seconds:
        return cached

    jobs = {
        "eastmoneyRank": ak.stock_hot_rank_em,
        "eastmoneyComment": ak.stock_comment_em,
        "xueqiu": lambda: ak.stock_hot_tweet_xq(symbol="最热门"),
        "weibo": lambda: ak.stock_js_weibo_report(time_period="CNHOUR24"),
    }
    frames: dict[str, object] = {}
    source_status: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fetcher): source for source, fetcher in jobs.items()}
        for future in as_completed(futures):
            source = futures[future]
            try:
                frames[source] = future.result()
                source_status[source] = "ok"
            except Exception as exc:  # Third-party market sources fail independently.
                source_status[source] = f"error:{exc.__class__.__name__}"

    profiles = _copy_profiles(cached.get("profiles"))
    weibo_by_name = _copy_profiles(cached.get("weiboByName"))

    eastmoney_frames = {
        source: frames[source]
        for source in ("eastmoneyRank", "eastmoneyComment")
        if source in frames
    }
    if eastmoney_frames:
        _clear_profile_source(profiles, "eastmoney")
        _merge_eastmoney_profiles(profiles, eastmoney_frames)
    else:
        _mark_cached(source_status, cached, "eastmoneyRank")
        _mark_cached(source_status, cached, "eastmoneyComment")

    if "xueqiu" in frames:
        _clear_profile_source(profiles, "xueqiu")
        _merge_xueqiu_profiles(profiles, frames["xueqiu"])
    else:
        _mark_cached(source_status, cached, "xueqiu")

    if "weibo" in frames:
        weibo_by_name = _build_weibo_profiles(frames["weibo"])
    else:
        _mark_cached(source_status, cached, "weibo")

    payload = {
        "fetchedAt": time.time(),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "profiles": profiles,
        "weiboByName": weibo_by_name,
        "sourceStatus": source_status,
    }
    _write_heat_cache(payload, cache_path)
    return payload


def apply_source_weights(row: dict, profile: dict | None = None) -> dict:
    result = dict(row)
    profile = profile or {}
    source_scores: dict[str, float | None] = {
        "guba": result.get("index") if result.get("postCount", 0) > 0 else None,
        "eastmoney": _profile_score(profile.get("eastmoney")),
        "xueqiu": _profile_score(profile.get("xueqiu")),
        "weibo": _profile_score(profile.get("weibo")),
    }
    blended, breakdown = blend_source_scores(source_scores)
    for source in breakdown:
        details = profile.get(source["source"])
        source["detail"] = str(details.get("detail", "")) if isinstance(details, dict) else ""

    result["baseIndex"] = result.get("index", 0)
    result["index"] = blended
    result["heatSources"] = breakdown
    result["scoreCoverage"] = round(
        sum(SOURCE_WEIGHTS[source["source"]] for source in breakdown),
        4,
    )
    result["sourceCount"] = len(breakdown)
    result["signal"] = interpret_index(blended)
    result["level"] = level_for(blended)
    return result


def blend_source_scores(scores: dict[str, float | None]) -> tuple[float, list[dict]]:
    available: list[tuple[str, float, float]] = []
    for source, weight in SOURCE_WEIGHTS.items():
        score = _safe_float(scores.get(source))
        if score is None:
            continue
        available.append((source, _clamp_score(score), weight))
    total_weight = sum(weight for _, _, weight in available)
    if total_weight <= 0:
        return 0.0, []

    blended = sum(score * weight for _, score, weight in available) / total_weight
    breakdown = [
        {
            "source": source,
            "label": SOURCE_LABELS[source],
            "score": round(score, 1),
            "appliedWeight": round(weight / total_weight, 4),
        }
        for source, score, weight in available
    ]
    return round(blended, 1), breakdown


def _merge_eastmoney_profiles(profiles: dict[str, dict], frames: dict[str, object]) -> None:
    components: dict[str, list[tuple[float, str]]] = {}
    rank_frame = frames.get("eastmoneyRank")
    if rank_frame is not None:
        for record in _frame_records(rank_frame):
            symbol = _normalize_symbol(record.get("代码"))
            rank = _safe_float(record.get("当前排名"))
            if not symbol or rank is None:
                continue
            score = _clamp_score(101 - rank)
            components.setdefault(symbol, []).append((score, f"人气榜第{int(rank)}"))

    comment_frame = frames.get("eastmoneyComment")
    if comment_frame is not None:
        for record in _frame_records(comment_frame):
            symbol = _normalize_symbol(record.get("代码"))
            attention = _safe_float(record.get("关注指数"))
            if not symbol or attention is None:
                continue
            score = _clamp_score(attention)
            components.setdefault(symbol, []).append((score, f"关注指数{score:.1f}"))

    for symbol, values in components.items():
        score = sum(value for value, _ in values) / len(values)
        profiles.setdefault(symbol, {})["eastmoney"] = {
            "score": round(score, 1),
            "detail": " / ".join(detail for _, detail in values),
        }


def _merge_xueqiu_profiles(profiles: dict[str, dict], frame: object) -> None:
    counts: dict[str, float] = {}
    for record in _frame_records(frame):
        symbol = _normalize_symbol(record.get("股票代码"))
        count = _safe_float(record.get("关注"))
        if symbol and count is not None:
            counts[symbol] = max(0, count)
    scores = _percentile_scores(counts)
    for symbol, score in scores.items():
        profiles.setdefault(symbol, {})["xueqiu"] = {
            "score": score,
            "detail": f"讨论{_compact_number(counts[symbol])}",
        }


def _build_weibo_profiles(frame: object) -> dict[str, dict]:
    records = _frame_records(frame)
    total = len(records)
    profiles: dict[str, dict] = {}
    for index, record in enumerate(records, start=1):
        name = _normalize_name(record.get("name"))
        if not name:
            continue
        score = 100 if total <= 1 else 100 - (index - 1) * 98 / (total - 1)
        profiles[name] = {
            "score": round(score, 1),
            "detail": f"24小时榜第{index}",
        }
    return profiles


def _percentile_scores(values: dict[str, float]) -> dict[str, float]:
    ordered = sorted(value for value in values.values() if value > 0)
    if not ordered:
        return {key: 0.0 for key in values}
    return {
        key: 0.0 if value <= 0 else round(bisect_right(ordered, value) / len(ordered) * 100, 1)
        for key, value in values.items()
    }


def _load_heat_cache(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_heat_cache(payload: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)
    except OSError:
        pass


def _copy_profiles(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): dict(profile)
        for key, profile in value.items()
        if isinstance(profile, dict)
    }


def _clear_profile_source(profiles: dict[str, dict], source: str) -> None:
    for profile in profiles.values():
        profile.pop(source, None)


def _mark_cached(source_status: dict[str, str], cached: dict, source: str) -> None:
    if cached and source_status.get(source, "").startswith("error:"):
        source_status[source] = source_status[source].replace("error:", "cached:", 1)


def _frame_records(frame: object) -> list[dict]:
    try:
        records = frame.to_dict("records")
    except (AttributeError, TypeError, ValueError):
        return []
    return [record for record in records if isinstance(record, dict)]


def _normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    match = re.search(r"(\d{6})", text)
    if match:
        return match.group(1)
    if text.isdigit() and len(text) <= 6:
        return text.zfill(6)
    return ""


def _normalize_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _profile_score(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    return _safe_float(value.get("score"))


def _safe_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _clamp_score(value: float) -> float:
    return max(0.0, min(100.0, value))


def _compact_number(value: float) -> str:
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return str(int(round(value)))


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
