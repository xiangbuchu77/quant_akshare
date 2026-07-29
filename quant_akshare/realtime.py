from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import time
from typing import Iterable
import urllib.request

import requests


EASTMONEY_SPOT_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
SPOT_FIELDS = ",".join(
    [
        "f12",
        "f14",
        "f2",
        "f3",
        "f4",
        "f5",
        "f6",
        "f7",
        "f8",
        "f9",
        "f10",
        "f15",
        "f16",
        "f17",
        "f18",
        "f20",
        "f21",
        "f23",
    ]
)


@dataclass(frozen=True)
class AlertRule:
    symbol: str
    below: float | None = None
    above: float | None = None
    label: str = "price alert"


@dataclass(frozen=True)
class WatchItem:
    symbol: str
    cost: float | None = None
    shares: float | None = None
    rules: tuple[AlertRule, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SpotQuote:
    symbol: str
    name: str
    price: float
    pct_change: float
    change: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: float
    amount: float
    turnover: float
    pe: float
    pb: float
    fetched_at: datetime


def market_prefix(symbol: str) -> str:
    if symbol.startswith(("5", "6", "9")):
        return "1"
    return "0"


def to_secid(symbol: str) -> str:
    return f"{market_prefix(symbol)}.{symbol}"


def tencent_prefix(symbol: str) -> str:
    if symbol.startswith(("5", "6", "9")):
        return "sh"
    if symbol.startswith("8"):
        return "bj"
    return "sz"


def parse_symbol_map(raw: str | None) -> dict[str, float]:
    if not raw:
        return {}
    result: dict[str, float] = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        symbol, value = item.split("=", 1)
        result[symbol.strip()] = float(value)
    return result


def fetch_spot_quotes(
    symbols: Iterable[str],
    timeout: float = 8.0,
    retries: int = 2,
    retry_delay: float = 0.5,
) -> list[SpotQuote]:
    try:
        return fetch_tencent_quotes(symbols, timeout=timeout)
    except Exception:
        return fetch_eastmoney_spot_quotes(
            symbols,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
        )


def fetch_tencent_quotes(
    symbols: Iterable[str],
    timeout: float = 8.0,
) -> list[SpotQuote]:
    symbol_list = [symbol.strip() for symbol in symbols if symbol.strip()]
    if not symbol_list:
        return []

    prefixed = [f"{tencent_prefix(symbol)}{symbol}" for symbol in symbol_list]
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        text = response.read().decode("gbk")

    fetched_at = datetime.now()
    quotes: list[SpotQuote] = []
    for line in text.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key[2:]
        quotes.append(
            SpotQuote(
                symbol=code,
                name=vals[1],
                price=_to_float(vals[3]),
                prev_close=_to_float(vals[4]),
                open=_to_float(vals[5]),
                change=_to_float(vals[31]),
                pct_change=_to_float(vals[32]),
                high=_to_float(vals[33]),
                low=_to_float(vals[34]),
                volume=0,
                amount=_to_float(vals[37]) * 10000,
                turnover=_to_float(vals[38]),
                pe=_to_float(vals[39]),
                pb=_to_float(vals[46]),
                fetched_at=fetched_at,
            )
        )
    return quotes


def fetch_eastmoney_spot_quotes(
    symbols: Iterable[str],
    timeout: float = 8.0,
    retries: int = 2,
    retry_delay: float = 0.5,
) -> list[SpotQuote]:
    symbol_list = [symbol.strip() for symbol in symbols if symbol.strip()]
    if not symbol_list:
        return []

    params = {
        "fltt": "2",
        "invt": "2",
        "fields": SPOT_FIELDS,
        "secids": ",".join(to_secid(symbol) for symbol in symbol_list),
    }
    session = requests.Session()
    session.trust_env = False
    last_error: requests.RequestException | None = None
    for attempt in range(retries + 1):
        try:
            response = session.get(EASTMONEY_SPOT_URL, params=params, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            break
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                raise
            time.sleep(retry_delay * (attempt + 1))
    else:
        raise RuntimeError("Failed to fetch spot quotes.") from last_error

    diff = payload.get("data", {}).get("diff") or []
    fetched_at = datetime.now()
    return [parse_spot_quote(item, fetched_at) for item in diff]


def _to_float(value: object) -> float:
    try:
        if value in (None, "", "-"):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_spot_quote(item: dict, fetched_at: datetime | None = None) -> SpotQuote:
    now = fetched_at or datetime.now()
    return SpotQuote(
        symbol=str(item.get("f12", "")),
        name=str(item.get("f14", "")),
        price=float(item.get("f2") or 0),
        pct_change=float(item.get("f3") or 0),
        change=float(item.get("f4") or 0),
        volume=float(item.get("f5") or 0),
        amount=float(item.get("f6") or 0),
        turnover=float(item.get("f8") or 0),
        pe=float(item.get("f9") or 0),
        high=float(item.get("f15") or 0),
        low=float(item.get("f16") or 0),
        open=float(item.get("f17") or 0),
        prev_close=float(item.get("f18") or 0),
        pb=float(item.get("f23") or 0),
        fetched_at=now,
    )


def build_watch_items(
    symbols: Iterable[str],
    costs: dict[str, float] | None = None,
    shares: dict[str, float] | None = None,
    stop_lines: dict[str, float] | None = None,
    target_lines: dict[str, float] | None = None,
) -> list[WatchItem]:
    costs = costs or {}
    shares = shares or {}
    stop_lines = stop_lines or {}
    target_lines = target_lines or {}
    items: list[WatchItem] = []
    for symbol in symbols:
        clean_symbol = symbol.strip()
        if not clean_symbol:
            continue
        rules: list[AlertRule] = []
        if clean_symbol in stop_lines:
            rules.append(
                AlertRule(
                    symbol=clean_symbol,
                    below=stop_lines[clean_symbol],
                    label="跌破风控线",
                )
            )
        if clean_symbol in target_lines:
            rules.append(
                AlertRule(
                    symbol=clean_symbol,
                    above=target_lines[clean_symbol],
                    label="突破目标线",
                )
            )
        items.append(
            WatchItem(
                symbol=clean_symbol,
                cost=costs.get(clean_symbol),
                shares=shares.get(clean_symbol),
                rules=tuple(rules),
            )
        )
    return items


def evaluate_alerts(quote: SpotQuote, item: WatchItem) -> list[str]:
    alerts: list[str] = []
    if item.cost:
        pnl = quote.price / item.cost - 1
        if pnl <= -0.03:
            alerts.append(f"成本浮亏 {pnl:.2%}")
        elif pnl >= 0.03:
            alerts.append(f"成本浮盈 {pnl:.2%}")
    if quote.price <= quote.prev_close * 0.97:
        alerts.append("日内跌幅超过 3%")
    if quote.price >= quote.prev_close * 1.03:
        alerts.append("日内涨幅超过 3%")
    for rule in item.rules:
        if rule.below is not None and quote.price <= rule.below:
            alerts.append(f"{rule.label}: {quote.price:.2f} <= {rule.below:.2f}")
        if rule.above is not None and quote.price >= rule.above:
            alerts.append(f"{rule.label}: {quote.price:.2f} >= {rule.above:.2f}")
    return alerts


def format_watch_row(quote: SpotQuote, item: WatchItem) -> str:
    pnl = "-"
    pnl_amount = ""
    market_value = ""
    if item.cost:
        pnl = f"{quote.price / item.cost - 1:+.2%}"
    if item.shares:
        market_value = f" | 市值 {quote.price * item.shares:.0f}"
        if item.cost:
            pnl_amount = f" | 浮盈亏 {(quote.price - item.cost) * item.shares:+.0f}"
    alerts = evaluate_alerts(quote, item)
    alert_text = "；".join(alerts) if alerts else "观察"
    return (
        f"{quote.fetched_at:%H:%M:%S} | {quote.symbol} {quote.name} | "
        f"现价 {quote.price:.2f} | 涨跌 {quote.pct_change:+.2f}% | "
        f"持仓 {item.shares or '-'} | 成本盈亏 {pnl}{pnl_amount}{market_value} | "
        f"成交额 {quote.amount / 100000000:.1f}亿 | "
        f"换手 {quote.turnover:.2f}% | {alert_text}"
    )
