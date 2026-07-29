from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import datetime
from html import escape
import io
import json
from pathlib import Path

import requests

from .retail_sentiment import fetch_retail_sentiment
from .realtime import SpotQuote, WatchItem, evaluate_alerts, fetch_spot_quotes, tencent_prefix, to_secid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRADE_LOG_PATH = PROJECT_ROOT / "data" / "qclaw_trades.jsonl"
DAILY_SNAPSHOT_PATH = PROJECT_ROOT / "data" / "daily_position_snapshots.json"
SECTOR_FLOW_URL = "https://push2.eastmoney.com/api/qt/clist/get"


@dataclass(frozen=True)
class PositionView:
    quote: SpotQuote
    item: WatchItem
    pnl_pct: float | None
    pnl_amount: float | None
    market_value: float | None
    advice: str
    advice_level: str


def build_position_view(quote: SpotQuote, item: WatchItem) -> PositionView:
    pnl_pct = quote.price / item.cost - 1 if item.cost is not None and item.cost > 0 else None
    pnl_amount = (
        (quote.price - item.cost) * item.shares
        if item.cost is not None and item.shares
        else None
    )
    market_value = quote.price * item.shares if item.shares else None
    advice, advice_level = make_advice(quote, item, pnl_pct)
    return PositionView(
        quote=quote,
        item=item,
        pnl_pct=pnl_pct,
        pnl_amount=pnl_amount,
        market_value=market_value,
        advice=advice,
        advice_level=advice_level,
    )


def make_advice(quote: SpotQuote, item: WatchItem, pnl_pct: float | None = None) -> tuple[str, str]:
    stop = next((rule.below for rule in item.rules if rule.below is not None), None)
    target = next((rule.above for rule in item.rules if rule.above is not None), None)
    if stop is not None and quote.price <= stop:
        return "跌破风控线，优先减仓或暂停加仓", "danger"
    if target is not None and quote.price >= target:
        return "到达目标线，考虑分批止盈", "good"
    if pnl_pct is not None and pnl_pct <= -0.10:
        return "浮亏超过 10%，先控制回撤", "danger"
    if pnl_pct is not None and pnl_pct <= -0.05:
        return "浮亏扩大，等待修复或降低仓位", "warn"
    if quote.pct_change <= -3:
        return "日内跌幅较大，观察是否放量破位", "warn"
    if pnl_pct is not None and pnl_pct >= 0.03:
        return "已有浮盈，持有并跟踪目标线", "good"
    return "未触发关键条件，继续观察", "neutral"


def render_dashboard(
    items: list[WatchItem],
    output_path: Path,
    title: str = "A股持仓决策看板",
    refresh_seconds: int = 5,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    symbols = [item.symbol for item in items]
    try:
        quotes = {quote.symbol: quote for quote in fetch_spot_quotes(symbols)}
    except requests.RequestException:
        # The static page can still refresh quotes in the browser after generation.
        quotes = {}
    views = [build_position_view(quotes[item.symbol], item) for item in items if item.symbol in quotes]
    payload = {
        "refreshSeconds": refresh_seconds,
        "market": [
            {"symbol": "sh000001", "name": "上证指数"},
            {"symbol": "sz399001", "name": "深成指"},
            {"symbol": "sz399006", "name": "创业板指"},
        ],
        "positions": [
            {
                "symbol": item.symbol,
                "cost": item.cost,
                "shares": item.shares,
                "stop": next((rule.below for rule in item.rules if rule.below is not None), None),
                "target": next((rule.above for rule in item.rules if rule.above is not None), None),
                "prefix": tencent_prefix(item.symbol),
                "secid": to_secid(item.symbol),
                "kind": "holding" if item.shares or item.cost else "watch",
            }
            for item in items
        ],
        "tradeDate": datetime.now().date().isoformat(),
        "todayTrades": _load_today_trades(),
        "dailyBaseline": _load_daily_baseline(),
        "accountSnapshot": _load_account_snapshot(),
        "sectorFlows": _fetch_sector_flows(),
        "retailSentiment": fetch_retail_sentiment(items, quotes),
        "initialQuotes": [
            {
                "symbol": view.quote.symbol,
                "name": view.quote.name,
                "price": view.quote.price,
                "prev": view.quote.prev_close,
                "open": view.quote.open,
                "change": view.quote.change,
                "pct": view.quote.pct_change,
                "high": view.quote.high,
                "low": view.quote.low,
                "amount": view.quote.amount,
                "turnover": view.quote.turnover,
                "pe": view.quote.pe,
                "pb": view.quote.pb,
            }
            for view in views
        ],
    }

    output_path.write_text(
        _html_document(title=title, views=views, payload=payload),
        encoding="utf-8",
    )
    return output_path


def _fetch_sector_flows() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for direction in (1, 0):
        for item in _fetch_sector_flow_page(direction):
            key = str(item.get("code") or item.get("name") or "")
            if key and key not in seen:
                seen.add(key)
                rows.append(item)
    if rows:
        return rows
    return _fetch_sector_flows_akshare()


def _fetch_sector_flow_page(direction: int) -> list[dict]:
    params = {
        "fid": "f62",
        "po": str(direction),
        "pz": "60",
        "pn": "1",
        "np": "1",
        "fltt": "2",
        "invt": "2",
        "fs": "m:90+t:2",
        "fields": "f12,f14,f3,f62,f184",
    }
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            SECTOR_FLOW_URL,
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
            timeout=8,
        )
        response.raise_for_status()
        diff = response.json().get("data", {}).get("diff") or []
    except (requests.RequestException, ValueError, TypeError):
        return []
    rows: list[dict] = []
    for item in diff:
        rows.append(
            {
                "code": str(item.get("f12") or ""),
                "name": str(item.get("f14") or ""),
                "pct": float(item.get("f3") or 0),
                "mainNet": float(item.get("f62") or 0),
                "mainNetRatio": float(item.get("f184") or 0),
                "updatedAt": datetime.now().strftime("%H:%M:%S"),
                "source": "server-cache",
            }
        )
    return [row for row in rows if row["name"]]


def _fetch_sector_flows_akshare() -> list[dict]:
    try:
        import akshare as ak

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            frame = ak.stock_fund_flow_industry(symbol="即时")
    except Exception:
        return []

    rows: list[dict] = []
    fetched_at = datetime.now().strftime("%H:%M:%S")
    for _, item in frame.iterrows():
        name = str(item.get("行业") or "").strip()
        if not name:
            continue
        net = _safe_float(item.get("净额"))
        pct = _safe_float(item.get("行业-涨跌幅"))
        rows.append(
            {
                "code": name,
                "name": name,
                "pct": pct,
                "mainNet": net * 100_000_000,
                "mainNetRatio": 0,
                "updatedAt": fetched_at,
                "source": "akshare-stock_fund_flow_industry",
            }
        )
    return rows


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value in (None, "", "-"):
            return default
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _load_today_trades(today: str | None = None) -> list[dict]:
    if not TRADE_LOG_PATH.exists():
        return []
    date_prefix = today or datetime.now().date().isoformat()
    records: list[dict] = []
    for line in TRADE_LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(record.get("time") or "").startswith(date_prefix):
            records.append(record)
    return records


def _load_daily_baseline(today: str | None = None) -> list[dict]:
    if not DAILY_SNAPSHOT_PATH.exists():
        return []
    try:
        snapshots = json.loads(DAILY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    date_key = today or datetime.now().date().isoformat()
    snapshot = snapshots.get(date_key) if isinstance(snapshots, dict) else None
    positions = snapshot.get("positions") if isinstance(snapshot, dict) else []
    return [item for item in positions if isinstance(item, dict)]


def _load_account_snapshot(today: str | None = None) -> dict | None:
    if not DAILY_SNAPSHOT_PATH.exists():
        return None
    try:
        snapshots = json.loads(DAILY_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    date_key = today or datetime.now().date().isoformat()
    snapshot = snapshots.get(date_key) if isinstance(snapshots, dict) else None
    account = snapshot.get("account") if isinstance(snapshot, dict) else None
    return account if isinstance(account, dict) else None


def _html_document(title: str, views: list[PositionView], payload: dict) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    total_value = sum(view.market_value or 0 for view in views)
    total_pnl = sum(view.pnl_amount or 0 for view in views)
    generated_at = views[0].quote.fetched_at.strftime("%Y-%m-%d %H:%M:%S") if views else ""
    rows = "\n".join(_initial_row(view) for view in views)
    cards = "\n".join(_initial_card(view) for view in views)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #18202b;
      --muted: #687385;
      --line: #dfe3ea;
      --red: #c92a2a;
      --green: #16794a;
      --amber: #9a6700;
      --blue: #1f5fbf;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif;
      letter-spacing: 0;
    }}
    header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }}
    h1 {{ margin: 0; font-size: 22px; font-weight: 700; }}
    .meta {{ color: var(--muted); font-size: 13px; white-space: nowrap; }}
    .sync-status {{ display: inline-block; margin-left: 8px; font-weight: 650; }}
    .sync-status.ok {{ color: var(--green); }}
    .sync-status.warn {{ color: var(--amber); }}
    .sync-status.down {{ color: var(--red); }}
    main {{ padding: 20px 24px 28px; max-width: 1280px; margin: 0 auto; }}
    .summary {{
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .metric {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .metric span {{ display: block; color: var(--muted); font-size: 12px; }}
    .metric strong {{ display: block; margin-top: 6px; font-size: 22px; }}
    .market-strip {{
      display: grid;
      grid-template-columns: repeat(3, minmax(160px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }}
    .market-pill {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      font-size: 14px;
    }}
    .sector-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .sentiment-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .sector-panel h2, .sentiment-panel h2 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    .sentiment-wrap {{
      display: grid;
      grid-template-columns: minmax(220px, 0.8fr) minmax(0, 1.6fr);
      gap: 12px;
    }}
    .sentiment-main, .sentiment-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 12px;
    }}
    .sentiment-main strong {{
      display: block;
      font-size: 28px;
      margin: 4px 0;
    }}
    .sentiment-main span, .sentiment-main p, .sentiment-card p {{
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
      margin: 4px 0 0;
    }}
    .sentiment-bars {{
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }}
    .sentiment-bar span {{
      display: flex;
      justify-content: space-between;
      font-size: 12px;
      color: var(--muted);
    }}
    .sentiment-track {{
      height: 7px;
      border-radius: 99px;
      background: #e9edf3;
      overflow: hidden;
    }}
    .sentiment-fill {{
      height: 100%;
      width: 0;
      background: var(--blue);
    }}
    .sentiment-fill.buy {{ background: var(--red); }}
    .sentiment-fill.sell {{ background: var(--green); }}
    .sentiment-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .sentiment-card h3 {{
      margin: 0 0 6px;
      font-size: 14px;
    }}
    .sentiment-card strong {{
      font-size: 20px;
    }}
    .sentiment-posts {{
      margin-top: 6px;
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 12px;
    }}
    .sentiment-sources {{
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
      margin-top: 8px;
    }}
    .sentiment-source {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      padding: 3px 6px;
      color: var(--muted);
      font-size: 11px;
      white-space: nowrap;
    }}
    .sentiment-source b {{
      color: var(--ink);
      font-size: 11px;
    }}
    .sector-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .sector-list {{
      display: grid;
      gap: 8px;
    }}
    .sector-list h3 {{
      margin: 0;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
    }}
    .sector-row {{
      display: grid;
      grid-template-columns: minmax(96px, 1fr) 80px 92px;
      align-items: center;
      gap: 8px;
      border-top: 1px solid var(--line);
      padding-top: 8px;
      font-size: 13px;
    }}
    .sector-row strong {{
      font-size: 14px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .kline-panel, .intraday-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .kline-panel h2, .intraday-panel h2 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    .kline-grid, .intraday-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(280px, 1fr));
      gap: 12px;
    }}
    .kline-card, .intraday-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfe;
    }}
    .kline-card {{
      position: relative;
    }}
    .kline-head, .intraday-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 8px;
      font-size: 13px;
    }}
    .kline-head strong, .intraday-head strong {{ font-size: 14px; }}
    .kline-head span, .intraday-head span {{ color: var(--muted); }}
    .chart-title {{
      display: grid;
      gap: 2px;
      min-width: 0;
    }}
    .chart-title strong {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .chart-title small {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.2;
    }}
    .kline-card canvas, .intraday-card canvas {{
      display: block;
      width: 100%;
      background: #fff;
      border: 1px solid #eef1f5;
      border-radius: 6px;
    }}
    .kline-card canvas {{
      height: 280px;
      cursor: crosshair;
    }}
    .intraday-card canvas {{
      height: 190px;
    }}
    .trade-panel, .manage-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .analysis-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .trade-panel h2, .manage-panel h2, .analysis-panel h2 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    .analysis-text {{
      color: var(--ink);
      line-height: 1.65;
      font-size: 14px;
    }}
    .analysis-cards {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .analysis-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 11px 12px;
    }}
    .analysis-card h3 {{
      margin: 0 0 7px;
      font-size: 14px;
      color: var(--blue);
    }}
    .analysis-card p {{
      margin: 0;
      color: var(--ink);
      line-height: 1.55;
    }}
    .analysis-card.risk h3 {{ color: var(--red); }}
    .analysis-card.plan h3 {{ color: var(--green); }}
    .analysis-card.full {{
      grid-column: 1 / -1;
    }}
    .analysis-actions {{
      display: flex;
      gap: 10px;
      align-items: center;
      margin: 8px 0 12px;
      flex-wrap: wrap;
    }}
    .analysis-actions button {{
      width: auto;
      min-width: 132px;
    }}
    .analysis-status {{
      color: var(--muted);
      font-size: 13px;
    }}
    .ai-chat {{
      border-top: 1px solid var(--line);
      margin-top: 12px;
      padding-top: 12px;
      display: grid;
      gap: 10px;
    }}
    .ai-chat-log {{
      display: grid;
      gap: 8px;
      max-height: 220px;
      overflow: auto;
    }}
    .ai-chat-empty {{
      color: var(--muted);
      font-size: 13px;
    }}
    .ai-message {{
      max-width: 86%;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      line-height: 1.5;
      font-size: 13px;
      background: #fbfcfe;
    }}
    .ai-message.user {{
      justify-self: end;
      background: #eef4ff;
      border-color: #bfd3f5;
      color: var(--blue);
    }}
    .ai-message.assistant {{
      justify-self: start;
    }}
    .ai-chat-form {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: end;
    }}
    .ai-chat-form input {{
      min-height: 38px;
    }}
    .ai-chat-form button {{
      width: 86px;
    }}
    .section-label {{
      margin: 14px 0 8px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 650;
    }}
    .today-panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 18px;
    }}
    .today-panel h2 {{
      margin: 0 0 12px;
      font-size: 16px;
    }}
    .today-head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 12px;
    }}
    .today-head h2 {{ margin: 0; }}
    .today-head button {{ width: auto; min-height: 30px; font-size: 12px; }}
    .today-summary {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }}
    .account-form {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr)) auto;
      gap: 10px;
      align-items: end;
      padding: 10px;
      margin-bottom: 4px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
    }}
    .today-metric, .today-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fbfcfe;
    }}
    .today-metric span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
    }}
    .today-metric strong {{
      display: block;
      margin-top: 4px;
      font-size: 18px;
    }}
    .today-metric small {{
      display: block;
      margin-top: 4px;
      color: #687385;
      font-size: 11px;
      line-height: 1.35;
    }}
    .today-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    .today-card {{
      font-size: 13px;
    }}
    .today-card h3 {{
      margin: 0 0 7px;
      font-size: 14px;
    }}
    .today-card p {{
      margin: 4px 0 0;
      color: var(--muted);
      line-height: 1.45;
    }}
    .today-card strong {{ font-size: 16px; }}
    .trade-log {{
      margin-top: 9px;
      border-top: 1px solid var(--line);
    }}
    .trade-log-row {{
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 8px;
      padding: 7px 0;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
    }}
    .trade-log-row:last-child {{ border-bottom: 0; }}
    .trade-log-row button {{ width: auto; min-height: 26px; padding: 3px 7px; font-size: 12px; }}
    .trade-log-side {{ font-weight: 700; }}
    .trade-form {{
      display: grid;
      grid-template-columns: 1.2fr 0.8fr 1fr 1fr auto auto;
      gap: 10px;
      align-items: end;
    }}
    .manage-grid {{
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(240px, 0.8fr);
      gap: 18px;
      align-items: end;
    }}
    .add-stock-form {{
      display: grid;
      grid-template-columns: 1fr 0.8fr 1fr 1fr auto;
      gap: 10px;
      align-items: end;
    }}
    .remove-watch-form {{
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: end;
    }}
    button.danger-button {{
      background: #fff2f2;
      color: var(--red);
    }}
    .field span {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }}
    select, input, button {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      padding: 6px 8px;
    }}
    button {{
      cursor: pointer;
      font-weight: 650;
      background: #eef4ff;
      color: var(--blue);
    }}
    button.secondary {{
      background: #f7f7f8;
      color: var(--muted);
    }}
    .trade-msg {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
      min-height: 18px;
    }}
    .table-wrap {{
      width: 100%;
      overflow-x: auto;
      border-radius: 8px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 11px 10px;
      border-bottom: 1px solid var(--line);
      text-align: right;
      font-size: 14px;
      white-space: nowrap;
    }}
    th:first-child, td:first-child, th:nth-child(2), td:nth-child(2), th:last-child, td:last-child {{
      text-align: left;
    }}
    th {{ color: var(--muted); font-weight: 600; background: #f9fafb; }}
    tr:last-child td {{ border-bottom: none; }}
    .cards {{ display: none; }}
    .up {{ color: var(--red); }}
    .down {{ color: var(--green); }}
    .neutral {{ color: var(--muted); }}
    .badge {{
      display: inline-flex;
      align-items: center;
      min-height: 26px;
      padding: 3px 8px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 650;
      border: 1px solid var(--line);
    }}
    .badge.good {{ color: var(--green); background: #eef9f2; border-color: #bfe5cd; }}
    .badge.warn {{ color: var(--amber); background: #fff8e8; border-color: #efd38a; }}
    .badge.danger {{ color: var(--red); background: #fff1f1; border-color: #efb8b8; }}
    .badge.neutral {{ color: var(--blue); background: #edf4ff; border-color: #bfd3f5; }}
    .volume-cell {{
      min-width: 132px;
      text-align: left;
    }}
    .volume-meter {{
      display: grid;
      gap: 5px;
      min-width: 116px;
    }}
    .volume-meter span {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .volume-track {{
      height: 7px;
      border-radius: 99px;
      background: #e9edf3;
      overflow: hidden;
    }}
    .volume-fill {{
      height: 100%;
      width: 10%;
      border-radius: inherit;
      background: var(--blue);
    }}
    .volume-fill.good {{ background: var(--red); }}
    .volume-fill.neutral {{ background: var(--blue); }}
    .volume-fill.warn {{ background: var(--amber); }}
    .volume-fill.danger {{ background: var(--green); }}
    .note {{ margin-top: 14px; color: var(--muted); font-size: 13px; line-height: 1.6; }}
    @media (max-width: 840px) {{
      header {{ align-items: flex-start; flex-direction: column; }}
      main {{ padding: 14px; }}
      .summary {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .market-strip {{ grid-template-columns: 1fr; }}
      .sector-grid {{ grid-template-columns: 1fr; }}
      .sentiment-wrap, .sentiment-grid {{ grid-template-columns: 1fr; }}
      .kline-grid, .intraday-grid {{ grid-template-columns: 1fr; }}
      .today-summary, .today-grid {{ grid-template-columns: 1fr; }}
      .account-form {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .analysis-cards {{ grid-template-columns: 1fr; }}
      .ai-message {{ max-width: 100%; }}
      .ai-chat-form {{ grid-template-columns: 1fr; }}
      .ai-chat-form button {{ width: 100%; }}
      .trade-form {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .manage-grid {{ grid-template-columns: 1fr; }}
      .add-stock-form {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      table {{ display: none; }}
      .cards {{ display: grid; gap: 10px; }}
      .stock-card {{
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 13px;
      }}
      .stock-card h2 {{ margin: 0 0 10px; font-size: 17px; }}
      .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }}
      .kv {{ border-top: 1px solid var(--line); padding-top: 8px; }}
      .kv span {{ display: block; color: var(--muted); font-size: 12px; }}
      .kv strong {{ display: block; margin-top: 2px; font-size: 15px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{escape(title)}</h1>
    <div class="meta">更新：<span id="updatedAt">{escape(generated_at)}</span> · 每 <span id="refreshText"></span> 秒刷新 <span class="sync-status warn" id="ledgerStatus">账本连接中</span></div>
  </header>
  <main>
    <section class="summary">
      <div class="metric"><span>持仓市值</span><strong id="totalValue">{_money(total_value)}</strong></div>
      <div class="metric"><span>浮动盈亏</span><strong id="totalPnl" class="{_num_class(total_pnl)}">{_money(total_pnl, signed=True)}</strong></div>
      <div class="metric"><span>大盘状态</span><strong id="marketState">读取中</strong></div>
      <div class="metric"><span>主要动作</span><strong id="topAction">{_top_action(views)}</strong></div>
    </section>
    <section class="market-strip" id="marketStrip"></section>
    <section class="sentiment-panel">
      <h2>宝妈指数 / 散户情绪温度计</h2>
      <div class="sentiment-wrap" id="retailSentiment">
        <div class="sentiment-main"><span>读取中</span><strong>-</strong><p>正在汇总股吧、东财、雪球与微博热度。</p></div>
      </div>
    </section>
    <section class="sector-panel">
      <h2>当日板块资金流向</h2>
      <div class="sector-grid" id="sectorFlow">
        <div class="sector-list"><h3>主力净流入</h3><div class="sector-row"><strong>读取中</strong><span>-</span><span>-</span></div></div>
        <div class="sector-list"><h3>主力净流出</h3><div class="sector-row"><strong>读取中</strong><span>-</span><span>-</span></div></div>
      </div>
    </section>
    <section class="kline-panel">
      <h2>持仓K线</h2>
      <div class="kline-grid" id="klineGrid"></div>
    </section>
    <section class="intraday-panel">
      <h2>持仓分时线</h2>
      <div class="intraday-grid" id="intradayGrid"></div>
    </section>
    <section class="analysis-panel">
      <h2>资金走势 / AI 分析摘要</h2>
      <div class="analysis-actions">
        <button type="button" id="aiAnalyze">DeepSeek 分析</button>
        <span class="analysis-status" id="aiStatus">AI 不会自动调用，点击按钮才会请求 DeepSeek。</span>
      </div>
      <div class="analysis-text" id="aiDigest">正在读取行情和资金流。</div>
      <div class="ai-chat">
        <div class="ai-chat-log" id="aiChatLog">
          <div class="ai-chat-empty">可以简短追问，比如“现在最需要盯哪只？”“游戏ETF要不要先减一点？”</div>
        </div>
        <form class="ai-chat-form" id="aiChatForm">
          <input id="aiQuestion" maxlength="120" autocomplete="off" placeholder="问一句盘中问题，例如：哪只风险最高？">
          <button type="submit" id="aiSend">发送</button>
        </form>
      </div>
    </section>
    <section class="today-panel">
      <div class="today-head"><h2>今日收益 / 做T建议</h2><button type="button" class="secondary" id="clearTodayTrades">清空今日记录</button></div>
      <form class="account-form" id="accountSnapshotForm">
        <label class="field"><span>券商总资产</span><input id="accountTotalAssets" type="number" step="0.01" min="0" placeholder="例如 80531.62" required></label>
        <label class="field"><span>券商总市值</span><input id="accountMarketValue" type="number" step="0.01" min="0" placeholder="例如 33892.00" required></label>
        <label class="field"><span>券商当日盈亏</span><input id="accountDailyPnl" type="number" step="0.01" placeholder="例如 326.00" required></label>
        <label class="field"><span>当日净转入</span><input id="accountNetTransfer" type="number" step="0.01" value="0" placeholder="转入为正，转出为负"></label>
        <button type="submit" id="saveAccountSnapshot">保存账户基准</button>
      </form>
      <div class="trade-msg" id="accountSnapshotMsg">账户口径以总资产为准；未设置时使用持仓与成交记录估算。</div>
      <div class="today-summary" id="todaySummary">
        <div class="today-metric"><span>今日交易</span><strong>读取中</strong></div>
        <div class="today-metric"><span>券商当日盈亏</span><strong>未设置</strong></div>
        <div class="today-metric"><span>持仓估算盈亏</span><strong>-</strong></div>
        <div class="today-metric"><span>已实现盈亏</span><strong>-</strong></div>
        <div class="today-metric"><span>当前浮盈亏</span><strong>-</strong></div>
        <div class="today-metric"><span>做T收益</span><strong>-</strong></div>
        <div class="today-metric"><span>买入 / 卖出金额</span><strong>-</strong></div>
      </div>
      <div class="today-grid" id="todayTrades"></div>
    </section>
    <section class="manage-panel">
      <h2>股票管理</h2>
      <div class="manage-grid">
        <form class="add-stock-form" id="addStockForm">
          <label class="field"><span>股票代码</span><input id="newStockSymbol" inputmode="numeric" maxlength="6" pattern="[0-9]{{6}}" placeholder="例如 000021" required></label>
          <label class="field"><span>加入类型</span><select id="newStockType"><option value="watch">自选</option><option value="holding">持仓</option></select></label>
          <label class="field"><span>持仓成本</span><input id="newStockCost" type="number" step="0.001" placeholder="例如 41.037" disabled></label>
          <label class="field"><span>持仓股数</span><input id="newStockShares" type="number" step="1" min="1" inputmode="numeric" placeholder="例如 300" disabled></label>
          <button type="submit" id="addStockButton">新增股票</button>
        </form>
        <form class="remove-watch-form" id="removeWatchForm">
          <label class="field"><span>删除自选</span><select id="removeWatchSymbol"></select></label>
          <button type="submit" class="danger-button" id="removeWatchButton">删除</button>
        </form>
      </div>
      <div class="trade-msg" id="stockManageMsg">新增持仓需要填写成本和股数；新增自选只需要股票代码。</div>
    </section>
    <section class="trade-panel">
      <h2>买入 / 卖出记录</h2>
      <form class="trade-form" id="tradeForm">
        <label class="field"><span>股票</span><select id="tradeSymbol"></select></label>
        <label class="field"><span>方向</span><select id="tradeSide"><option value="buy">买入</option><option value="sell">卖出</option></select></label>
        <label class="field"><span>成交价</span><input id="tradePrice" type="number" step="0.001" min="0" placeholder="例如 1.088" required></label>
        <label class="field"><span>股数</span><input id="tradeShares" type="number" step="1" min="1" inputmode="numeric" placeholder="例如 900" required></label>
        <button type="submit">更新持仓</button>
        <button type="button" class="secondary" id="resetPositions">重置</button>
      </form>
      <div class="trade-msg" id="tradeMsg">这里记录的是看板持仓；自选股买入后会自动转为持仓，不会向券商下单。</div>
    </section>
    <div class="section-label">持仓与自选观察</div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>类型</th><th>代码</th><th>名称</th><th>现价</th><th>涨跌幅</th><th>量能</th><th>成本</th><th>股数</th>
            <th>市值</th><th>浮盈亏</th><th>止损</th><th>支撑位</th><th>目标</th><th>PE</th><th>PB</th>
            <th>东财主力净流入</th><th>吸筹</th><th>动态线</th><th>建议</th>
          </tr>
        </thead>
        <tbody id="rows">{rows}</tbody>
      </table>
    </div>
    <section class="cards" id="cards">{cards}</section>
    <p class="note">提示：看板只做交易辅助，不自动下单。止损/目标是参考线，最终建议会结合上证、深成指、创业板指的实时状态动态调整。资金流使用东方财富分钟资金流口径，可能与同花顺主力口径不一致；最终持仓成本以券商显示为准。</p>
  </main>
  <script>
    const DASHBOARD = {payload_json};
    const STORAGE_KEY = "quantDashboardPositions";
    const AI_ANALYSIS_KEY = "quantDashboardAiAnalysis";
    const AI_CHAT_KEY = "quantDashboardAiChat";
    const FLOW_REFRESH_SECONDS = 60;
    const AI_ENDPOINT = "http://127.0.0.1:18765/analyze";
    const AI_CHAT_ENDPOINT = "http://127.0.0.1:18765/chat";
    const QCLAW_ENDPOINT = "http://127.0.0.1:18766/qclaw/message";
    const QCLAW_SNAPSHOT_ENDPOINT = "http://127.0.0.1:18766/api/snapshot";
    const DASHBOARD_TRADE_DATE = DASHBOARD.tradeDate || "";
    const defaultPositions = DASHBOARD.positions;
    const defaultPositionKey = positionKey(defaultPositions);
    let dailyBaseline = Array.isArray(DASHBOARD.dailyBaseline) ? DASHBOARD.dailyBaseline : [];
    let positions = loadPositions();
    let todayTrades = normalizeTodayTrades(DASHBOARD.todayTrades || []);
    let latestPortfolioMetrics = null;
    let latestAccountSnapshot = DASHBOARD.accountSnapshot || null;
    let latestSnapshotTradeDate = DASHBOARD_TRADE_DATE;
    let latestLedgerRevision = null;
    let latestQuotes = Object.fromEntries((DASHBOARD.initialQuotes || []).map(q => [q.symbol, q]));
    let latestMarketQuotes = {{}};
    let latestFlows = {{}};
    let latestVolumeProfiles = {{}};
    let latestIntradayProfiles = {{}};
    let latestSectorFlows = DASHBOARD.sectorFlows || [];
    let latestRetailSentiment = DASHBOARD.retailSentiment || null;
    let klineChartState = {{}};
    const marketSymbols = DASHBOARD.market;
    document.getElementById("refreshText").textContent = DASHBOARD.refreshSeconds;

    function currentTradeDate(date = new Date()) {{
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${{year}}-${{month}}-${{day}}`;
    }}
    function localDateTimeString(date = new Date()) {{
      const hours = String(date.getHours()).padStart(2, "0");
      const minutes = String(date.getMinutes()).padStart(2, "0");
      const seconds = String(date.getSeconds()).padStart(2, "0");
      return `${{currentTradeDate(date)}}T${{hours}}:${{minutes}}:${{seconds}}`;
    }}
    function tradeDateKey(value) {{
      const raw = String(value || "");
      const match = raw.match(/^\\d{{4}}-\\d{{2}}-\\d{{2}}/);
      if (match) return match[0];
      const parsed = raw ? new Date(raw) : null;
      if (parsed && !Number.isNaN(parsed.getTime())) return currentTradeDate(parsed);
      return "";
    }}
    function normalizeTodayTrades(records) {{
      const tradeDate = currentTradeDate();
      return (records || []).filter(record => tradeDateKey(record.time) === tradeDate);
    }}
    function appendTodayTrade(trade) {{
      const record = {{ ...trade, id: trade.id || newTradeId(), time: trade.time || localDateTimeString() }};
      if (tradeDateKey(record.time) !== currentTradeDate()) return;
      todayTrades.push(record);
      renderTodayTrades(latestQuotes);
    }}
    function newTradeId() {{
      if (window.crypto && typeof window.crypto.randomUUID === "function") return window.crypto.randomUUID();
      return `web-${{Date.now()}}-${{Math.random().toString(16).slice(2)}}`;
    }}

    function loadPositions() {{
      try {{
        const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
        if (Array.isArray(saved) && saved.length && positionKey(saved) === defaultPositionKey) {{
          return saved;
        }}
        localStorage.removeItem(STORAGE_KEY);
        return defaultPositions.map(p => ({{ ...p }}));
      }} catch (_err) {{
        return defaultPositions.map(p => ({{ ...p }}));
      }}
    }}
    function positionKey(list) {{
      return (list || []).map(p => [
        p.kind || "",
        p.symbol,
        p.cost ?? "",
        p.shares ?? "",
        p.stop ?? "",
        p.target ?? ""
      ].join(":")).join("|");
    }}
    function savePositions() {{
      localStorage.setItem(STORAGE_KEY, JSON.stringify(positions));
    }}
    async function resetPositions() {{
      const synced = await syncDynamicSnapshot();
      setTradeMessage(synced ? "已恢复为账本中的最新持仓。" : "账本服务未连接，未修改当前持仓。", synced ? "neutral" : "warn");
    }}
    function prefixForSymbol(symbol) {{
      if (symbol.startsWith("5") || symbol.startsWith("6") || symbol.startsWith("9")) return "sh";
      if (symbol.startsWith("8")) return "bj";
      return "sz";
    }}
    function renderTradeOptions(preferredSymbol = "") {{
      const select = document.getElementById("tradeSymbol");
      const previousSymbol = preferredSymbol || select.value;
      select.innerHTML = positions.map(p => `<option value="${{p.symbol}}">${{escapeHtml(displayNameForSymbol(p.symbol))}} · ${{p.symbol}}</option>`).join("");
      if (previousSymbol && positions.some(p => p.symbol === previousSymbol)) {{
        select.value = previousSymbol;
      }}
    }}
    function renderStockManagement() {{
      const select = document.getElementById("removeWatchSymbol");
      const button = document.getElementById("removeWatchButton");
      const watches = positions.filter(isWatch);
      select.innerHTML = watches.map(p => `<option value="${{p.symbol}}">${{escapeHtml(displayNameForSymbol(p.symbol))}} · ${{p.symbol}}</option>`).join("");
      select.disabled = !watches.length;
      button.disabled = !watches.length;
    }}
    function updateNewStockFields() {{
      const isHolding = document.getElementById("newStockType").value === "holding";
      const cost = document.getElementById("newStockCost");
      const shares = document.getElementById("newStockShares");
      cost.disabled = !isHolding;
      shares.disabled = !isHolding;
      cost.required = isHolding;
      shares.required = isHolding;
    }}
    function setStockManageMessage(text, level = "neutral") {{
      const el = document.getElementById("stockManageMsg");
      el.textContent = text;
      el.className = `trade-msg ${{level}}`;
    }}
    async function postQClawAction(action, payload) {{
      const response = await fetch(QCLAW_ENDPOINT, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ action, payload }})
      }});
      const data = await response.json().catch(() => ({{}}));
      if (!response.ok || data.type === "error") throw new Error(data.error || `服务返回 ${{response.status}}`);
      return data;
    }}
    async function addStock(event) {{
      event.preventDefault();
      const button = document.getElementById("addStockButton");
      const symbol = document.getElementById("newStockSymbol").value.trim();
      const type = document.getElementById("newStockType").value;
      const cost = Number(document.getElementById("newStockCost").value);
      const shares = Number(document.getElementById("newStockShares").value);
      if (!/^\\d{{6}}$/.test(symbol)) {{
        setStockManageMessage("请输入 6 位股票代码。", "down");
        return;
      }}
      if (positions.some(position => position.symbol === symbol)) {{
        setStockManageMessage("这只股票已经在持仓或自选中；持仓数量请通过买入/卖出记录调整。", "warn");
        return;
      }}
      if (type === "holding" && (!Number.isFinite(cost) || !Number.isFinite(shares) || shares <= 0)) {{
        setStockManageMessage("新增持仓需要填写有效成本和股数。", "down");
        return;
      }}
      button.disabled = true;
      setStockManageMessage("正在写入组合账本。");
      try {{
        const data = type === "holding"
          ? await postQClawAction("apply_trade", {{ symbol, side: "buy", price: cost, shares }})
          : await postQClawAction("import_watchlist", {{ symbols: [symbol] }});
        syncPositionsFromState(data.state);
        await syncDynamicSnapshot();
        document.getElementById("newStockSymbol").value = "";
        document.getElementById("newStockCost").value = "";
        document.getElementById("newStockShares").value = "";
        setStockManageMessage(
          type === "holding"
            ? `${{displayNameForSymbol(symbol)}}已按今日买入加入持仓和成交记录。`
            : `${{displayNameForSymbol(symbol)}}已加入自选。`,
          "up"
        );
      }} catch (error) {{
        setStockManageMessage(`新增失败：${{error.message}}`, "down");
      }} finally {{
        button.disabled = false;
      }}
    }}
    async function removeWatch(event) {{
      event.preventDefault();
      const select = document.getElementById("removeWatchSymbol");
      const button = document.getElementById("removeWatchButton");
      const symbol = select.value;
      if (!symbol) return;
      if (!window.confirm(`从自选中删除 ${{displayNameForSymbol(symbol)}}？`)) return;
      button.disabled = true;
      try {{
        const data = await postQClawAction("remove_watchlist", {{ symbols: [symbol] }});
        syncPositionsFromState(data.state);
        await syncDynamicSnapshot();
        setStockManageMessage(`${{displayNameForSymbol(symbol)}}已从自选删除。`, "neutral");
      }} catch (error) {{
        setStockManageMessage(`删除失败：${{error.message}}`, "down");
      }} finally {{
        button.disabled = false;
      }}
    }}
    function setTradeMessage(text, level = "neutral") {{
      const el = document.getElementById("tradeMsg");
      el.textContent = text;
      el.className = `trade-msg ${{level}}`;
    }}
    function isWatch(p) {{
      return p.kind === "watch" || (!Number(p.shares || 0) && !Number(p.cost || 0));
    }}
    function typeLabel(p) {{
      return isWatch(p) ? "自选" : "持仓";
    }}
    function saveAiAnalysis(text) {{
      localStorage.setItem(AI_ANALYSIS_KEY, JSON.stringify({{
        text,
        savedAt: new Date().toISOString()
      }}));
    }}
    function loadAiAnalysis() {{
      try {{
        const saved = JSON.parse(localStorage.getItem(AI_ANALYSIS_KEY) || "null");
        return saved && saved.text ? saved : null;
      }} catch (_err) {{
        return null;
      }}
    }}
    function loadAiChat() {{
      try {{
        const rows = JSON.parse(localStorage.getItem(AI_CHAT_KEY) || "[]");
        return Array.isArray(rows) ? rows.slice(-10) : [];
      }} catch (_err) {{
        return [];
      }}
    }}
    function saveAiChat(rows) {{
      localStorage.setItem(AI_CHAT_KEY, JSON.stringify((rows || []).slice(-10)));
    }}
    function renderAiChat() {{
      const log = document.getElementById("aiChatLog");
      const rows = loadAiChat();
      if (!rows.length) {{
        log.innerHTML = `<div class="ai-chat-empty">可以简短追问，比如“现在最需要盯哪只？”“游戏ETF要不要先减一点？”</div>`;
        return;
      }}
      log.innerHTML = rows.map(item => `
        <div class="ai-message ${{item.role === "user" ? "user" : "assistant"}}">${{escapeHtml(item.content)}}</div>
      `).join("");
      log.scrollTop = log.scrollHeight;
    }}
    function appendAiChat(role, content) {{
      const rows = loadAiChat();
      rows.push({{ role, content: String(content || ""), time: new Date().toISOString() }});
      saveAiChat(rows);
      renderAiChat();
    }}

    function num(v, digits = 2) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
      return Number(v).toFixed(digits);
    }}
    function isEtfItem(item) {{
      const symbol = String(item && item.symbol || "");
      const name = String(item && item.name || "");
      return /ETF|基金/.test(name) || /^(15|16|18|50|51|56|58)/.test(symbol);
    }}
    function priceNum(v, item = null) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
      const n = Number(v);
      if (isEtfItem(item)) return n.toFixed(3);
      return Math.abs(Math.round(n * 1000) - Math.round(n * 100) * 10) > 0 ? n.toFixed(3) : n.toFixed(2);
    }}
    function money(v, signed = false) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
      const sign = signed && v > 0 ? "+" : "";
      return sign + Math.round(v).toLocaleString("zh-CN");
    }}
    function wan(v, signed = false) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
      const n = Number(v) / 10000;
      const sign = signed && n > 0 ? "+" : "";
      return sign + n.toFixed(Math.abs(n) >= 1000 ? 0 : 1) + "万";
    }}
    function yi(v, signed = false) {{
      if (v === null || v === undefined || Number.isNaN(Number(v))) return "-";
      const n = Number(v) / 100000000;
      const sign = signed && n > 0 ? "+" : "";
      return sign + n.toFixed(Math.abs(n) >= 100 ? 0 : 2) + "亿";
    }}
    function cls(v) {{
      if (Number(v) > 0) return "up";
      if (Number(v) < 0) return "down";
      return "neutral";
    }}
    function marketRegime(marketQuotes) {{
      const list = Object.values(marketQuotes);
      if (!list.length) return {{ state: "未知", level: "neutral", avg: 0 }};
      const avg = list.reduce((sum, q) => sum + q.pct, 0) / list.length;
      const weakCount = list.filter(q => q.pct <= -1).length;
      const strongCount = list.filter(q => q.pct >= 0.8).length;
      if (weakCount >= 2 || avg <= -1) return {{ state: "弱势", level: "danger", avg }};
      if (strongCount >= 2 || avg >= 0.8) return {{ state: "强势", level: "good", avg }};
      return {{ state: "震荡", level: "neutral", avg }};
    }}
    function flowScore(flow, q, regime) {{
      if (!flow || !flow.latest) return {{ score: 50, label: "资金读取中", level: "neutral", mainNet: null, trend: null, updatedAt: "" }};
      const mainNet = flow.latest.mainNet;
      const bigNet = flow.latest.largeNet + flow.latest.superLargeNet;
      const trend = flow.trendMainNet;
      const updatedAt = flow.updatedAt || flow.latest.time || "";
      let score = 50;
      score += mainNet > 0 ? 16 : -16;
      score += trend > 0 ? 16 : -16;
      score += bigNet > 0 ? 14 : -14;
      if (q.pct >= -1.5 && q.pct <= 2.2) score += 10;
      if (q.pct > 4) score -= 10;
      if (q.pct < -3) score -= 12;
      if (regime.state === "强势") score += 6;
      if (regime.state === "弱势") score -= 8;
      score = Math.max(0, Math.min(100, Math.round(score)));
      if (score >= 75) return {{ score, label: "主力吸筹增强", level: "good", mainNet, trend, updatedAt }};
      if (score >= 60) return {{ score, label: "温和流入", level: "good", mainNet, trend, updatedAt }};
      if (score >= 45) return {{ score, label: "资金分歧", level: "neutral", mainNet, trend, updatedAt }};
      if (score >= 30) return {{ score, label: "资金转弱", level: "warn", mainNet, trend, updatedAt }};
      return {{ score, label: "疑似派发", level: "danger", mainNet, trend, updatedAt }};
    }}
    function tradingProgress() {{
      const now = new Date();
      const minutes = now.getHours() * 60 + now.getMinutes();
      const morningStart = 9 * 60 + 30;
      const morningEnd = 11 * 60 + 30;
      const afternoonStart = 13 * 60;
      const afternoonEnd = 15 * 60;
      let elapsed = 0;
      if (minutes <= morningStart) elapsed = 0;
      else if (minutes <= morningEnd) elapsed = minutes - morningStart;
      else if (minutes <= afternoonStart) elapsed = 120;
      else if (minutes <= afternoonEnd) elapsed = 120 + minutes - afternoonStart;
      else elapsed = 240;
      return Math.max(0.05, Math.min(1, elapsed / 240));
    }}
    function volumeEval(q, profile) {{
      if (!q) {{
        return {{ ratio: null, projectedAmount: null, avgAmount: null, label: "量能读取中", level: "neutral", width: 8 }};
      }}
      if (!profile || !profile.avgAmount) {{
        const turnover = Number(q.turnover || 0);
        let label = "平量观察";
        let level = "neutral";
        let width = Math.max(8, Math.min(100, turnover * 16));
        if (turnover >= 5) {{
          label = "高换手放量";
          level = "good";
        }} else if (turnover >= 3) {{
          label = "温和放量";
          level = "good";
        }} else if (turnover <= 0.6) {{
          label = "低换手缩量";
          level = "danger";
        }} else if (turnover <= 1.2) {{
          label = "略缩量";
          level = "warn";
        }}
        return {{ ratio: null, projectedAmount: q.amount, avgAmount: null, label, level, width }};
      }}
      const projectedAmount = q.amount / tradingProgress();
      const ratio = projectedAmount / profile.avgAmount;
      let label = "平量";
      let level = "neutral";
      if (ratio >= 1.8) {{
        label = "明显放量";
        level = "good";
      }} else if (ratio >= 1.2) {{
        label = "温和放量";
        level = "good";
      }} else if (ratio <= 0.65) {{
        label = "明显缩量";
        level = "danger";
      }} else if (ratio <= 0.85) {{
        label = "略缩量";
        level = "warn";
      }}
      return {{
        ratio,
        projectedAmount,
        avgAmount: profile.avgAmount,
        label,
        level,
        width: Math.max(8, Math.min(100, Math.round(ratio * 50)))
      }};
    }}
    function renderVolumeMeter(evalData) {{
      const ratioText = evalData.ratio === null ? "-" : `${{evalData.ratio.toFixed(2)}}x`;
      return `
        <div class="volume-meter">
          <span><strong class="${{evalData.level === "good" ? "up" : (evalData.level === "danger" ? "down" : "neutral")}}">${{evalData.label}}</strong><em>${{ratioText}}</em></span>
          <div class="volume-track"><div class="volume-fill ${{evalData.level}}" style="width:${{evalData.width}}%"></div></div>
        </div>`;
    }}
    function dynamicLines(p, q, pnlPct, regime, flowEval) {{
      const baseStop = p.stop || (Number(p.cost) > 0 ? p.cost * 0.94 : null);
      const baseTarget = p.target || q.price * 1.04;
      let stop = baseStop;
      let target = baseTarget;
      let note = "沿用参考线";
      if (flowEval.score >= 75 && regime.state !== "弱势") {{
        stop = Math.max(baseStop || 0, q.price * 0.965);
        target = Math.max(baseTarget, q.price * 1.045);
        note = "吸筹增强，上移防守线";
      }} else if (flowEval.score < 45 || regime.state === "弱势") {{
        stop = Math.max(baseStop || 0, q.price * 0.982);
        target = Math.min(baseTarget, q.price * 1.025);
        note = "资金偏弱，收紧防守";
      }} else if (pnlPct !== null && pnlPct > 0.03) {{
        stop = Math.max(baseStop || 0, q.price * 0.975);
        note = "已有浮盈，保护利润";
      }}
      return {{ stop, target, note }};
    }}
    function supportEval(p, q, profile, lines) {{
      const candidates = [];
      if (profile && profile.recentLow) candidates.push({{ value: profile.recentLow, label: "5日低点" }});
      if (lines && lines.stop) candidates.push({{ value: lines.stop, label: "动态防守" }});
      if (p.cost && p.cost <= q.price * 1.015) candidates.push({{ value: p.cost, label: "成本附近" }});
      if (q.low && q.low <= q.price * 1.01) candidates.push({{ value: q.low, label: "日内低点" }});
      const valid = candidates
        .filter(item => Number(item.value) > 0 && Number(item.value) <= q.price * 1.02)
        .sort((a, b) => b.value - a.value);
      const picked = valid[0];
      if (!picked) return {{ value: null, label: "支撑读取中" }};
      const distance = q.price ? (q.price / picked.value - 1) * 100 : null;
      return {{ value: picked.value, label: picked.label, distance }};
    }}
    function advice(q, p, pnlPct, regime, flowEval) {{
      const weak = regime.state === "弱势";
      const strong = regime.state === "强势";
      const underCost = pnlPct !== null && pnlPct < 0;
      if (isWatch(p)) {{
        if (q.pct >= 5) return ["自选观察：日内涨幅过大，不追高，等回踩", "warn"];
        if (flowEval.score >= 75 && !weak && q.pct <= 3) return ["自选观察：吸筹增强，可等分时回踩试探", "good"];
        if (flowEval.score < 45 || weak) return ["自选观察：资金或大盘偏弱，暂不急入手", "warn"];
        return ["自选观察：条件一般，等待放量突破或回踩确认", "neutral"];
      }}
      if (p.stop && q.price <= p.stop) return [weak ? "大盘弱且跌破参考风控，优先减仓" : "跌破参考风控，先降风险观察", "danger"];
      if (pnlPct !== null && pnlPct <= -0.10) return [weak ? "大盘弱且浮亏超过10%，先控制回撤" : "浮亏超过10%，不加仓，等修复", "danger"];
      if (pnlPct !== null && pnlPct <= -0.05) return [weak ? "大盘弱，浮亏扩大，降低仓位优先" : "浮亏扩大，等企稳再处理", "warn"];
      if (q.pct <= -3) return [weak ? "跟随大盘下杀，先看是否放量破位" : "个股日内大跌，观察承接", "warn"];
      if (p.target && q.price >= p.target) return [strong ? "大盘强，突破参考目标，分批止盈不清仓" : "到达参考目标，考虑分批止盈", "good"];
      if (flowEval.score >= 75 && q.pct < 3) return [underCost ? "吸筹增强但仍低于成本，可等确认修复" : "吸筹增强，持有并上移防守线", "good"];
      if (flowEval.score < 45) return [weak ? "大盘弱且资金转弱，先防守" : "资金转弱，减少追涨动作", "warn"];
      if (pnlPct !== null && pnlPct >= 0.03) return [weak ? "有浮盈但大盘弱，优先保护利润" : "已有浮盈，按大盘强弱决定持有力度", "good"];
      if (strong && !underCost) return ["大盘强，未触发风险，继续持有观察", "good"];
      if (weak && underCost) return ["大盘弱且仍在成本下，控制仓位", "warn"];
      return ["震荡环境，按参考线观察，不机械交易", "neutral"];
    }}
    function parseTencentLine(line) {{
      if (!line || !line.includes("=") || !line.includes('"')) return null;
      const key = line.split("=")[0].split("_").pop();
      const vals = line.split('"')[1].split("~");
      if (vals.length < 53) return null;
      return {{
        symbol: key.slice(2),
        name: vals[1],
        price: Number(vals[3]) || 0,
        prev: Number(vals[4]) || 0,
        open: Number(vals[5]) || 0,
        change: Number(vals[31]) || 0,
        pct: Number(vals[32]) || 0,
        high: Number(vals[33]) || 0,
        low: Number(vals[34]) || 0,
        amount: (Number(vals[37]) || 0) * 10000,
        turnover: Number(vals[38]) || 0,
        pe: Number(vals[39]) || 0,
        pb: Number(vals[46]) || 0
      }};
    }}
    function renderMarket(marketQuotes, regime) {{
      const stateEl = document.getElementById("marketState");
      stateEl.textContent = `${{regime.state}} ${{regime.avg >= 0 ? "+" : ""}}${{regime.avg.toFixed(2)}}%`;
      stateEl.className = regime.level === "good" ? "up" : (regime.level === "danger" ? "down" : "neutral");
      document.getElementById("marketStrip").innerHTML = marketSymbols.map(m => {{
        const q = marketQuotes[m.symbol];
        if (!q) return "";
        return `<div class="market-pill"><span>${{m.name}}</span><strong class="${{cls(q.pct)}}">${{q.pct > 0 ? "+" : ""}}${{num(q.pct)}}%</strong></div>`;
      }}).join("");
    }}
    function renderSectorFlows(sectors) {{
      const list = Array.isArray(sectors) ? sectors : [];
      const inflow = list.filter(item => item.mainNet > 0).slice().sort((a, b) => b.mainNet - a.mainNet).slice(0, 6);
      const outflow = list.filter(item => item.mainNet < 0).slice().sort((a, b) => a.mainNet - b.mainNet).slice(0, 6);
      const renderList = (title, rows) => `
        <div class="sector-list">
          <h3>${{title}}</h3>
          ${{rows.length ? rows.map(item => `
            <div class="sector-row">
              <strong title="${{item.name}}">${{item.name}}</strong>
              <span class="${{cls(item.pct)}}">${{item.pct > 0 ? "+" : ""}}${{num(item.pct)}}%</span>
              <span class="${{cls(item.mainNet)}}">${{yi(item.mainNet, true)}}</span>
            </div>`).join("") : '<div class="sector-row"><strong>暂无数据</strong><span>-</span><span>-</span></div>'}}
        </div>`;
      document.getElementById("sectorFlow").innerHTML = renderList("主力净流入", inflow) + renderList("主力净流出", outflow);
    }}
    function sentimentLevelClass(level) {{
      if (level === "danger") return "down";
      if (level === "warn") return "neutral";
      if (level === "good" || level === "cool") return "up";
      return "neutral";
    }}
    function renderRetailSentiment(data) {{
      const target = document.getElementById("retailSentiment");
      if (!target) return;
      const overall = data && data.overall ? data.overall : null;
      if (!overall) {{
        target.innerHTML = `<div class="sentiment-main"><span>多来源散户情绪</span><strong>-</strong><p>暂无散户情绪数据。</p></div>`;
        return;
      }}
      const items = Array.isArray(data.items) ? data.items : [];
      const itemHtml = items.map(item => {{
        const sourceScores = (item.heatSources || []).map(source => `
          <span class="sentiment-source" title="${{escapeHtml((source.detail || "") + " · 当前权重 " + Math.round((source.appliedWeight || 0) * 100) + "%")}}">
            ${{escapeHtml(source.label || source.source)}} <b>${{num(source.score, 1)}}</b>
          </span>`).join("");
        return `
          <div class="sentiment-card">
            <h3>${{escapeHtml(item.name || item.symbol)}} <small>${{escapeHtml(item.boardCode || item.symbol)}}</small></h3>
            <strong class="${{sentimentLevelClass(item.level)}}">${{num(item.index, 1)}}</strong>
            <p>${{escapeHtml(item.signal || "")}} · ${{item.postCount || 0}}帖 · 小白占比 ${{num(item.newbieRatio || 0, 1)}}%</p>
            <div class="sentiment-sources">${{sourceScores || '<span class="sentiment-source">暂无跨平台旁证</span>'}}</div>
            <div class="sentiment-bars">
              <div class="sentiment-bar"><span><em>追涨温度</em><b>${{num(item.buyIndex || 0, 1)}}</b></span><div class="sentiment-track"><div class="sentiment-fill buy" style="width:${{Math.min(100, item.buyIndex || 0)}}%"></div></div></div>
              <div class="sentiment-bar"><span><em>割肉温度</em><b>${{num(item.sellIndex || 0, 1)}}</b></span><div class="sentiment-track"><div class="sentiment-fill sell" style="width:${{Math.min(100, item.sellIndex || 0)}}%"></div></div></div>
            </div>
            <div class="sentiment-posts">${{(item.topPosts || []).slice(0, 2).map(post => `<div>${{escapeHtml(post.title)}} · ${{escapeHtml(post.intent || "neutral")}}</div>`).join("") || escapeHtml(item.error ? item.error + "，已使用跨平台热度" : "暂无典型小白帖")}}</div>
          </div>`;
      }}).join("");
      target.innerHTML = `
        <div class="sentiment-main">
          <span>${{escapeHtml((data.source || "多来源散户情绪") + " · " + (data.updatedAt || ""))}}</span>
          <strong class="${{sentimentLevelClass(overall.level)}}">${{num(overall.index, 1)}}</strong>
          <p>${{escapeHtml(overall.signal || "")}} · ${{overall.trackedCount || items.length}}只标的 · ${{overall.sourceCount || 0}}个有效来源 · 股吧样本 ${{overall.postCount || 0}}帖</p>
          <div class="sentiment-bars">
            <div class="sentiment-bar"><span><em>宝妈买入/追涨</em><b>${{num(overall.buyIndex || 0, 1)}}</b></span><div class="sentiment-track"><div class="sentiment-fill buy" style="width:${{Math.min(100, overall.buyIndex || 0)}}%"></div></div></div>
            <div class="sentiment-bar"><span><em>宝妈卖出/割肉</em><b>${{num(overall.sellIndex || 0, 1)}}</b></span><div class="sentiment-track"><div class="sentiment-fill sell" style="width:${{Math.min(100, overall.sellIndex || 0)}}%"></div></div></div>
          </div>
        </div>
        <div class="sentiment-grid">${{itemHtml || '<div class="sentiment-card"><h3>暂无标的</h3><p>导入持仓或自选后显示散户情绪。</p></div>'}}</div>`;
    }}
    function escapeHtml(value) {{
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }}
    function displayNameForSymbol(symbol) {{
      const q = latestQuotes[symbol];
      return q && q.name ? q.name : symbol;
    }}
    function replaceSymbolsWithNames(text) {{
      let output = String(text || "");
      for (const p of positions) {{
        const name = displayNameForSymbol(p.symbol);
        if (name && name !== p.symbol) {{
          output = output.replaceAll(p.symbol, name);
        }}
      }}
      return output;
    }}
    function analysisClass(title) {{
      if (/风险|失效|减仓|卖出/.test(title)) return "risk";
      if (/操作|计划|建议|优先级/.test(title)) return "plan";
      return "";
    }}
    function renderAiAnalysis(text) {{
      const cleaned = replaceSymbolsWithNames(text || "").replace(/\\*\\*/g, "").trim();
      const target = document.getElementById("aiDigest");
      if (!cleaned) {{
        target.textContent = "AI 没有返回内容。";
        return;
      }}
      const normalized = cleaned
        .replace(/(?:^|\\n)\\s*(\\d+)[.、]\\s*/g, "\\n$1. ")
        .trim();
      const matches = Array.from(normalized.matchAll(/(?:^|\\n)(\\d+)\\.\\s*([^：:\\n]+)[：:]?\\s*([\\s\\S]*?)(?=\\n\\d+\\.\\s*[^：:\\n]+[：:]?|\\s*$)/g));
      if (!matches.length) {{
        target.innerHTML = `<div class="analysis-cards"><div class="analysis-card full"><h3>AI 分析</h3><p>${{escapeHtml(cleaned)}}</p></div></div>`;
        return;
      }}
      target.innerHTML = `<div class="analysis-cards">${{matches.map(match => {{
        const title = match[2].trim();
        const body = match[3].trim().replace(/\\n+/g, " ");
        return `<div class="analysis-card ${{analysisClass(title)}}"><h3>${{escapeHtml(title)}}</h3><p>${{escapeHtml(body)}}</p></div>`;
      }}).join("")}}</div>`;
    }}
    function renderAiDigest(regime, flowEvals) {{
      const entries = flowEvals.filter(item => item.q);
      const saved = loadAiAnalysis();
      const strongFlows = entries.filter(item => item.flowEval.score >= 75);
      const weakFlows = entries.filter(item => item.flowEval.score < 45);
      const best = entries.slice().sort((a, b) => b.flowEval.score - a.flowEval.score)[0];
      const risk = entries.slice().sort((a, b) => a.flowEval.score - b.flowEval.score)[0];
      const marketText = `大盘当前为${{regime.state}}，三大指数平均涨跌幅 ${{regime.avg >= 0 ? "+" : ""}}${{regime.avg.toFixed(2)}}%。`;
      const flowText = strongFlows.length
        ? `资金上最强的是 ${{strongFlows.map(x => x.p.symbol + " " + x.q.name).join("、")}}，可以重点观察回踩承接。`
        : (weakFlows.length ? `有 ${{weakFlows.length}} 只资金偏弱，先把防守线放在优先级前面。` : "资金没有明显单边信号，按震荡策略处理。");
      const riskText = risk ? `当前最需要盯的是 ${{risk.p.symbol}}，吸筹分 ${{risk.flowEval.score}}，动态防守参考 ${{num(risk.lines.stop)}}。` : "";
      const gptText = "DeepSeek 接入方式：点击按钮时才会通过本地 AI 服务直连 DeepSeek 生成实时文字复盘。";
      if (saved) {{
        renderAiAnalysis(saved.text);
        document.getElementById("aiStatus").textContent = `已保留上次 DeepSeek 分析 · ${{new Date(saved.savedAt).toLocaleString("zh-CN", {{ hour12: false }})}}`;
      }} else {{
        renderAiAnalysis(`1. 盘面结论：${{marketText}} 2. 资金与量能：${{flowText}} 3. 持仓优先级：${{riskText || "等待行情刷新后排序。"}} 4. 操作计划：${{gptText}}`);
      }}
    }}
    function tradeSideLabel(side) {{
      return side === "sell" ? "卖出" : "买入";
    }}
    function tradeGroups() {{
      const groups = new Map();
      todayTrades = normalizeTodayTrades(todayTrades);
      for (const raw of todayTrades) {{
        const symbol = String(raw.symbol || "");
        if (!symbol) continue;
        const side = String(raw.side || "buy");
        const price = Number(raw.price || 0);
        const shares = Number(raw.shares || 0);
        if (!price || !shares) continue;
        if (!groups.has(symbol)) {{
          groups.set(symbol, {{
            symbol,
            trades: [],
            buyShares: 0,
            sellShares: 0,
            buyAmount: 0,
            sellAmount: 0
          }});
        }}
        const group = groups.get(symbol);
        group.trades.push({{
          id: String(raw.id || ""), side, price, shares, time: raw.time || "",
          costBasis: Number(raw.cost_basis || raw.costBasis || 0)
        }});
        if (side === "sell") {{
          group.sellShares += shares;
          group.sellAmount += price * shares;
        }} else {{
          group.buyShares += shares;
          group.buyAmount += price * shares;
        }}
      }}
      return Array.from(groups.values());
    }}
    function tAdvice(group, quote) {{
      const avgBuy = group.buyShares ? group.buyAmount / group.buyShares : null;
      const avgSell = group.sellShares ? group.sellAmount / group.sellShares : null;
      const current = quote ? quote.price : null;
      if (avgBuy && avgSell) {{
        const diff = avgSell - avgBuy;
        if (diff > 0) return `今日T差为正，均卖价高于均买价 ${{diff.toFixed(3)}}，后续不追高，等回踩均价线再做。`;
        if (diff < 0) return `今日T差为负，均买价高于均卖价 ${{Math.abs(diff).toFixed(3)}}，先停止加仓，等分时重新站上均价。`;
        return "今日买卖价差接近持平，后续只做明确回踩和冲高。";
      }}
      if (avgSell && current) {{
        if (current < avgSell) return `卖出后价格低于均卖价 ${{avgSell.toFixed(3)}}，T出暂时有效，等缩量回踩再考虑接回。`;
        return `卖出后价格高于均卖价 ${{avgSell.toFixed(3)}}，不要急追，等分时回落或放量确认。`;
      }}
      if (avgBuy && current) {{
        if (current > avgBuy) return `买入后价格高于均买价 ${{avgBuy.toFixed(3)}}，低吸暂时有效，跌回均价线下要收缩。`;
        return `买入后价格低于均买价 ${{avgBuy.toFixed(3)}}，先看止损和分时均价，不继续摊。`;
      }}
      return "今日有交易记录，等待行情刷新后给出做T建议。";
    }}
    function dashboardTradeDayIsCurrent() {{
      return String(latestSnapshotTradeDate || DASHBOARD.tradeDate || "") === currentTradeDate();
    }}
    async function refreshForNewTradeDay() {{
      if (dashboardTradeDayIsCurrent()) return;
      try {{
        const response = await fetch(QCLAW_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "generate_dashboard", payload: {{}} }})
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok || data.type === "error") throw new Error(data.error || `服务返回 ${{response.status}}`);
        window.location.reload();
      }} catch (_error) {{
        const grid = document.getElementById("todayTrades");
        if (grid) grid.innerHTML = `<div class="today-card"><h3>等待今日看板生成</h3><p>检测到交易日已切换。请启动 QClaw 本地服务后刷新页面，避免使用昨日持仓基准。</p></div>`;
      }}
    }}
    function dailyAccountPnl(quotes, totalBuy, totalSell) {{
      const baseline = new Map(dailyBaseline.map(item => [String(item.symbol || ""), Number(item.shares || 0)]));
      let currentValue = 0;
      let previousCloseValue = 0;
      for (const position of positions) {{
        const quote = quotes[position.symbol] || latestQuotes[position.symbol];
        if (!quote) continue;
        const currentShares = Number(position.shares || 0);
        const openingShares = Number(baseline.get(position.symbol) || 0);
        currentValue += currentShares * Number(quote.price || 0);
        previousCloseValue += openingShares * Number(quote.prev || quote.price || 0);
      }}
      for (const [symbol, shares] of baseline.entries()) {{
        if (positions.some(position => position.symbol === symbol)) continue;
        const quote = quotes[symbol] || latestQuotes[symbol];
        previousCloseValue += shares * Number(quote && (quote.prev || quote.price) || 0);
      }}
      return {{ value: currentValue + totalSell - previousCloseValue - totalBuy, hasBaseline: baseline.size > 0 }};
    }}
    function renderTodayTrades(quotes) {{
      if (!dashboardTradeDayIsCurrent()) {{
        document.getElementById("todaySummary").innerHTML = `
          <div class="today-metric"><span>今日交易 · ${{currentTradeDate()}}</span><strong>待刷新</strong></div>
          <div class="today-metric"><span>券商当日盈亏</span><strong>等待今日基准</strong></div>
          <div class="today-metric"><span>持仓估算盈亏</span><strong>等待今日基准</strong></div>
          <div class="today-metric"><span>已实现盈亏</span><strong>-</strong></div>
          <div class="today-metric"><span>当前浮盈亏</span><strong>-</strong></div>
          <div class="today-metric"><span>做T收益</span><strong>-</strong></div>
          <div class="today-metric"><span>买入 / 卖出金额</span><strong>-</strong></div>`;
        return;
      }}
      const groups = tradeGroups();
      const totalBuy = groups.reduce((sum, group) => sum + group.buyAmount, 0);
      const totalSell = groups.reduce((sum, group) => sum + group.sellAmount, 0);
      const accountPnl = dailyAccountPnl(quotes, totalBuy, totalSell);
      const metrics = latestPortfolioMetrics;
      const dailyPnl = metrics ? metrics.dailyPnl : accountPnl.value;
      const dailyComplete = metrics ? metrics.complete : accountPnl.hasBaseline;
      const accountMode = metrics && metrics.dailyPnlSource === "account";
      const positionDailyPnl = metrics ? metrics.positionDailyPnl : accountPnl.value;
      const positionPnlComplete = metrics ? metrics.positionPnlComplete !== false : accountPnl.hasBaseline;
      const positionIssues = metrics && Array.isArray(metrics.positionUnreconciled) ? metrics.positionUnreconciled : [];
      const positionIssueText = positionIssues.map(item => {{
        const difference = Number(item.difference || 0);
        const action = difference > 0 ? `新增${{difference}}股未记成交` : `减少${{Math.abs(difference)}}股未记成交`;
        return `${{displayNameForSymbol(String(item.symbol || ""))}}：${{action}}`;
      }}).join("；");
      const dailyRate = metrics ? metrics.dailyPnlRate : null;
      const realizedPnl = metrics ? metrics.realizedPnl : null;
      const unrealizedPnl = metrics ? metrics.unrealizedPnl : null;
      const tProfit = metrics ? metrics.tProfit : groups.reduce((sum, group) => {{
        const avgBuy = group.buyShares ? group.buyAmount / group.buyShares : null;
        const avgSell = group.sellShares ? group.sellAmount / group.sellShares : null;
        return sum + (avgBuy && avgSell ? (avgSell - avgBuy) * Math.min(group.buyShares, group.sellShares) : 0);
      }}, 0);
      const buyAmount = metrics ? metrics.buyAmount : totalBuy;
      const sellAmount = metrics ? metrics.sellAmount : totalSell;
      const tradeCount = metrics ? metrics.tradeCount : todayTrades.length;
      document.getElementById("todaySummary").innerHTML = `
        <div class="today-metric"><span>今日交易 · ${{currentTradeDate()}}</span><strong>${{tradeCount}} 笔</strong></div>
        <div class="today-metric"><span>券商当日盈亏${{dailyRate === null ? "" : " · " + (dailyRate * 100).toFixed(2) + "%"}}</span><strong class="${{cls(dailyPnl)}}">${{accountMode && dailyComplete && dailyPnl !== null ? money(dailyPnl, true) : "请录入账户基准"}}</strong></div>
        <div class="today-metric"><span>持仓估算盈亏${{positionPnlComplete ? "" : " · 仅已核对"}}</span><strong class="${{cls(positionDailyPnl)}}">${{positionDailyPnl === null ? "无法估算" : money(positionDailyPnl, true)}}</strong>${{positionIssueText ? `<small>${{escapeHtml(positionIssueText)}}</small>` : ""}}</div>
        <div class="today-metric"><span>已实现盈亏（今日卖出）</span><strong class="${{cls(realizedPnl)}}">${{realizedPnl === null ? "-" : money(realizedPnl, true)}}</strong></div>
        <div class="today-metric"><span>当前浮盈亏</span><strong class="${{cls(unrealizedPnl)}}">${{unrealizedPnl === null ? "-" : money(unrealizedPnl, true)}}</strong></div>
        <div class="today-metric"><span>做T收益（成交配对）</span><strong class="${{cls(tProfit)}}">${{metrics && !metrics.tMatchedShares ? "未配对" : money(tProfit, true)}}</strong></div>
        <div class="today-metric"><span>买入 / 卖出金额</span><strong>${{money(buyAmount)}} / ${{money(sellAmount)}}</strong></div>`;
      const grid = document.getElementById("todayTrades");
      if (!groups.length) {{
        grid.innerHTML = `<div class="today-card"><h3>今日暂无买卖记录</h3><p>本板块按本机日期每日重新结算；有买卖记录后，这里会按股票汇总做T结果。</p></div>`;
        return;
      }}
      grid.innerHTML = groups.map(group => {{
        const quote = quotes[group.symbol] || latestQuotes[group.symbol];
        const avgBuy = group.buyShares ? group.buyAmount / group.buyShares : null;
        const avgSell = group.sellShares ? group.sellAmount / group.sellShares : null;
        const paired = Math.min(group.buyShares, group.sellShares);
        const profit = avgBuy && avgSell ? (avgSell - avgBuy) * paired : null;
        const logRows = group.trades.slice().sort((a, b) => String(b.time).localeCompare(String(a.time))).map(item => `
          <div class="trade-log-row">
            <span class="trade-log-side ${{item.side === "sell" ? "down" : "up"}}">${{tradeSideLabel(item.side)}}</span>
            <span>${{item.time ? item.time.slice(11, 16) + " · " : ""}}${{item.shares}}股 @ ${{item.price.toFixed(3)}}</span>
            <button type="button" class="secondary" data-revoke-trade="${{escapeHtml(item.id)}}" ${{item.id ? "" : "disabled"}}>撤回</button>
          </div>`).join("");
        return `
          <div class="today-card">
            <h3>${{escapeHtml(displayNameForSymbol(group.symbol))}} <small>${{group.symbol}}</small></h3>
            <strong class="${{cls(profit)}}">${{profit === null ? "未形成完整T" : money(profit, true)}}</strong>
            <p>买入 ${{group.buyShares || 0}}股${{avgBuy ? "，均价 " + avgBuy.toFixed(3) : ""}}；卖出 ${{group.sellShares || 0}}股${{avgSell ? "，均价 " + avgSell.toFixed(3) : ""}}。</p>
            <p>${{escapeHtml(tAdvice(group, quote))}}</p>
            <div class="trade-log">${{logRows}}</div>
          </div>`;
      }}).join("");
      grid.querySelectorAll("[data-revoke-trade]").forEach(button => {{
        button.addEventListener("click", () => revokeTrade(button.dataset.revokeTrade));
      }});
    }}
    function renderKlineShell() {{
      const holdings = positions.filter(p => !isWatch(p));
      const grid = document.getElementById("klineGrid");
      if (!holdings.length) {{
        grid.innerHTML = `<div class="kline-card"><div class="kline-head"><strong>暂无持仓</strong><span>-</span></div><canvas id="kline-empty" width="720" height="280"></canvas></div>`;
        drawKlineChart(document.getElementById("kline-empty"), [], null);
        return;
      }}
      grid.innerHTML = holdings.map(p => `
        <div class="kline-card">
          <div class="kline-head"><div class="chart-title"><strong>${{escapeHtml(displayNameForSymbol(p.symbol))}}</strong><small>${{p.symbol}}</small></div><span>近30日 · 日K</span></div>
          <canvas id="kline-${{p.symbol}}" width="720" height="280"></canvas>
        </div>`).join("");
      for (const p of holdings) bindKlineInteraction(document.getElementById(`kline-${{p.symbol}}`), p);
    }}
    function drawKlineChart(canvas, rows, p, hoverIndex = null) {{
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      const ratio = window.devicePixelRatio || 1;
      const box = canvas.getBoundingClientRect();
      const width = Math.max(320, Math.floor(box.width || canvas.width));
      const height = Math.max(240, Math.floor(box.height || canvas.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      const data = (rows || []).filter(row => row.open && row.close && row.high && row.low).slice(-30);
      if (!data.length) {{
        ctx.fillStyle = "#687385";
        ctx.font = "13px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText("K线读取中", 16, 30);
        return;
      }}
      const pad = {{ left: 52, right: 18, top: 18, bottom: 30 }};
      const highs = data.map(row => row.high);
      const lows = data.map(row => row.low);
      if (p && p.stop) lows.push(Number(p.stop));
      if (p && p.target) highs.push(Number(p.target));
      const rawMax = Math.max(...highs);
      const rawMin = Math.min(...lows);
      const margin = Math.max(0.01, (rawMax - rawMin) * 0.08);
      const maxPrice = rawMax + margin;
      const minPrice = Math.max(0, rawMin - margin);
      const span = Math.max(0.01, maxPrice - minPrice);
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const y = price => pad.top + ((maxPrice - price) / span) * plotH;
      ctx.strokeStyle = "#edf0f5";
      ctx.lineWidth = 1;
      ctx.fillStyle = "#687385";
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      for (let i = 0; i <= 4; i++) {{
        const yy = pad.top + (plotH * i / 4);
        const price = maxPrice - (span * i / 4);
        ctx.beginPath();
        ctx.moveTo(pad.left, yy);
        ctx.lineTo(width - pad.right, yy);
        ctx.stroke();
        ctx.fillText(priceNum(price, p), 6, yy + 4);
      }}
      const step = plotW / data.length;
      const bodyW = Math.max(5, Math.min(18, step * 0.62));
      data.forEach((row, i) => {{
        const x = pad.left + step * i + step / 2;
        const rising = row.close >= row.open;
        const color = rising ? "#c92a2a" : "#16794a";
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.35;
        ctx.beginPath();
        ctx.moveTo(x, y(row.high));
        ctx.lineTo(x, y(row.low));
        ctx.stroke();
        const top = y(Math.max(row.open, row.close));
        const bottom = y(Math.min(row.open, row.close));
        const bodyH = Math.max(2, bottom - top);
        if (i === hoverIndex) {{
          ctx.fillStyle = "rgba(31, 95, 191, 0.08)";
          ctx.fillRect(x - step / 2, pad.top, step, plotH);
          ctx.fillStyle = color;
        }}
        if (rising) {{
          ctx.strokeRect(x - bodyW / 2, top, bodyW, bodyH);
        }} else {{
          ctx.fillRect(x - bodyW / 2, top, bodyW, bodyH);
        }}
      }});
      if (p && p.stop) drawKlineLine(ctx, width, pad, y(Number(p.stop)), "#16794a", `止损 ${{priceNum(p.stop, p)}}`);
      if (p && p.target) drawKlineLine(ctx, width, pad, y(Number(p.target)), "#c92a2a", `目标 ${{priceNum(p.target, p)}}`);
      klineChartState[canvas.id] = {{ data, p, rows, pad, plotW, plotH, step, width, height, y }};
      if (hoverIndex !== null && data[hoverIndex]) {{
        drawKlineHover(ctx, data[hoverIndex], hoverIndex, klineChartState[canvas.id]);
      }}
      ctx.fillStyle = "#687385";
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText(data[0].date.slice(5), pad.left, height - 7);
      ctx.fillText(data[data.length - 1].date.slice(5), Math.max(pad.left + 72, width - pad.right - 42), height - 7);
    }}
    function bindKlineInteraction(canvas, p) {{
      if (!canvas) return;
      canvas.onmousemove = event => {{
        const state = klineChartState[canvas.id];
        if (!state || !state.data.length) return;
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const index = Math.max(0, Math.min(state.data.length - 1, Math.floor((x - state.pad.left) / state.step)));
        drawKlineChart(canvas, state.rows, p, index);
      }};
      canvas.onmouseleave = () => {{
        const state = klineChartState[canvas.id];
        if (state) drawKlineChart(canvas, state.rows, p, null);
      }};
    }}
    function drawKlineHover(ctx, row, index, state) {{
      const x = state.pad.left + state.step * index + state.step / 2;
      const yClose = state.y(row.close);
      ctx.save();
      ctx.strokeStyle = "rgba(31, 95, 191, 0.55)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, state.pad.top);
      ctx.lineTo(x, state.height - state.pad.bottom);
      ctx.moveTo(state.pad.left, yClose);
      ctx.lineTo(state.width - state.pad.right, yClose);
      ctx.stroke();
      ctx.setLineDash([]);
      const changePct = row.pct !== null && row.pct !== undefined ? Number(row.pct) : ((row.close / row.open - 1) * 100);
      const lines = [
        `${{row.date}}  ${{changePct >= 0 ? "+" : ""}}${{changePct.toFixed(2)}}%`,
        `开 ${{priceNum(row.open, state.p)}}  高 ${{priceNum(row.high, state.p)}}`,
        `低 ${{priceNum(row.low, state.p)}}  收 ${{priceNum(row.close, state.p)}}`
      ];
      ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
      const boxW = Math.max(...lines.map(line => ctx.measureText(line).width)) + 18;
      const boxH = 58;
      const boxX = x + boxW + 18 > state.width ? x - boxW - 12 : x + 12;
      const boxY = Math.max(state.pad.top + 4, Math.min(yClose - 28, state.height - state.pad.bottom - boxH - 4));
      ctx.fillStyle = "rgba(24, 32, 43, 0.92)";
      ctx.strokeStyle = "rgba(255,255,255,0.22)";
      roundRect(ctx, boxX, boxY, boxW, boxH, 8);
      ctx.fill();
      ctx.stroke();
      lines.forEach((line, i) => {{
        ctx.fillStyle = i === 0 ? "#ffffff" : "#dce3ef";
        ctx.fillText(line, boxX + 9, boxY + 17 + i * 17);
      }});
      ctx.restore();
    }}
    function roundRect(ctx, x, y, w, h, r) {{
      ctx.beginPath();
      ctx.moveTo(x + r, y);
      ctx.lineTo(x + w - r, y);
      ctx.quadraticCurveTo(x + w, y, x + w, y + r);
      ctx.lineTo(x + w, y + h - r);
      ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
      ctx.lineTo(x + r, y + h);
      ctx.quadraticCurveTo(x, y + h, x, y + h - r);
      ctx.lineTo(x, y + r);
      ctx.quadraticCurveTo(x, y, x + r, y);
      ctx.closePath();
    }}
    function drawKlineLine(ctx, width, pad, yValue, color, label) {{
      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = color;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(pad.left, yValue);
      ctx.lineTo(width - pad.right, yValue);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText(label, width - pad.right - 66, yValue - 4);
      ctx.restore();
    }}
    function renderKlineCharts() {{
      for (const p of positions.filter(item => !isWatch(item))) {{
        const profile = latestVolumeProfiles[p.symbol];
        drawKlineChart(document.getElementById(`kline-${{p.symbol}}`), profile ? profile.rows : [], p);
      }}
    }}
    function renderIntradayShell() {{
      const holdings = positions.filter(p => !isWatch(p));
      const grid = document.getElementById("intradayGrid");
      if (!holdings.length) {{
        grid.innerHTML = `<div class="intraday-card"><div class="intraday-head"><strong>暂无持仓</strong><span>-</span></div><canvas id="intraday-empty" width="520" height="190"></canvas></div>`;
        drawIntradayChart(document.getElementById("intraday-empty"), [], null, null);
        return;
      }}
      grid.innerHTML = holdings.map(p => `
        <div class="intraday-card">
          <div class="intraday-head"><div class="chart-title"><strong>${{escapeHtml(displayNameForSymbol(p.symbol))}}</strong><small>${{p.symbol}}</small></div><span>今日分时 · 价格/均价</span></div>
          <canvas id="intraday-${{p.symbol}}" width="520" height="190"></canvas>
        </div>`).join("");
    }}
    function drawIntradayChart(canvas, rows, p, quote) {{
      if (!canvas) return;
      const ctx = canvas.getContext("2d");
      const ratio = window.devicePixelRatio || 1;
      const box = canvas.getBoundingClientRect();
      const width = Math.max(320, Math.floor(box.width || canvas.width));
      const height = Math.max(170, Math.floor(box.height || canvas.height));
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      const data = (rows || []).filter(row => row.price > 0);
      if (!data.length) {{
        ctx.fillStyle = "#687385";
        ctx.font = "13px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.fillText("分时读取中", 16, 30);
        return;
      }}
      const pad = {{ left: 42, right: 14, top: 14, bottom: 24 }};
      const values = data.flatMap(row => [row.price, row.avg || row.price]);
      if (quote && quote.prev) values.push(quote.prev);
      if (p && p.stop) values.push(Number(p.stop));
      if (p && p.target) values.push(Number(p.target));
      const maxPrice = Math.max(...values);
      const minPrice = Math.min(...values);
      const span = Math.max(0.01, maxPrice - minPrice);
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      const x = index => pad.left + (plotW * index / Math.max(1, data.length - 1));
      const y = price => pad.top + ((maxPrice - price) / span) * plotH;
      ctx.strokeStyle = "#edf0f5";
      ctx.lineWidth = 1;
      ctx.fillStyle = "#687385";
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      for (let i = 0; i <= 3; i++) {{
        const yy = pad.top + (plotH * i / 3);
        const price = maxPrice - (span * i / 3);
        ctx.beginPath();
        ctx.moveTo(pad.left, yy);
        ctx.lineTo(width - pad.right, yy);
        ctx.stroke();
        ctx.fillText(price.toFixed(2), 4, yy + 4);
      }}
      if (quote && quote.prev) drawKlineLine(ctx, width, pad, y(Number(quote.prev)), "#687385", "昨收");
      drawIntradayPolyline(ctx, data, row => row.avg || row.price, x, y, "#d49a1f", 1.4);
      drawIntradayPolyline(ctx, data, row => row.price, x, y, "#1f5fbf", 1.8);
      if (p && p.stop) drawKlineLine(ctx, width, pad, y(Number(p.stop)), "#16794a", `止损 ${{priceNum(p.stop, p)}}`);
      if (p && p.target) drawKlineLine(ctx, width, pad, y(Number(p.target)), "#c92a2a", `目标 ${{priceNum(p.target, p)}}`);
      const last = data[data.length - 1];
      ctx.fillStyle = "#18202b";
      ctx.font = "12px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.fillText(`${{last.time.slice(11, 16)}}  ${{priceNum(last.price, p)}}`, pad.left, height - 7);
      ctx.fillStyle = "#d49a1f";
      ctx.fillText("均价", Math.max(pad.left + 92, width - pad.right - 94), height - 7);
      ctx.fillStyle = "#1f5fbf";
      ctx.fillText("价格", Math.max(pad.left + 138, width - pad.right - 46), height - 7);
    }}
    function drawIntradayPolyline(ctx, data, pick, x, y, color, lineWidth) {{
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = lineWidth;
      ctx.beginPath();
      data.forEach((row, index) => {{
        const xx = x(index);
        const yy = y(pick(row));
        if (index === 0) ctx.moveTo(xx, yy);
        else ctx.lineTo(xx, yy);
      }});
      ctx.stroke();
      ctx.restore();
    }}
    function renderIntradayCharts() {{
      for (const p of positions.filter(item => !isWatch(item))) {{
        const profile = latestIntradayProfiles[p.symbol];
        drawIntradayChart(
          document.getElementById(`intraday-${{p.symbol}}`),
          profile ? profile.rows : [],
          p,
          latestQuotes[p.symbol] || null
        );
      }}
    }}
    function intradayEvaluation(p, q) {{
      const profile = latestIntradayProfiles[p.symbol];
      const rows = profile && Array.isArray(profile.rows) ? profile.rows : [];
      const last = rows[rows.length - 1];
      const first = rows[0];
      if (!last || !q) {{
        return {{ label: "分时读取中", level: "neutral", lastPrice: null, avgPrice: null, aboveAvg: null }};
      }}
      const avg = Number(last.avg || 0);
      const price = Number(last.price || q.price || 0);
      const openPrice = Number(first.price || price);
      const high = Math.max(...rows.map(row => Number(row.price || 0)).filter(Boolean));
      const low = Math.min(...rows.map(row => Number(row.price || 0)).filter(Boolean));
      const aboveAvg = avg ? price >= avg : null;
      const pullbackFromHigh = high ? (price / high - 1) * 100 : 0;
      const moveFromOpen = openPrice ? (price / openPrice - 1) * 100 : 0;
      let label = "分时震荡";
      let level = "neutral";
      if (aboveAvg && moveFromOpen >= 0.5 && pullbackFromHigh > -1.2) {{
        label = "站上均价，承接尚可";
        level = "good";
      }} else if (aboveAvg && pullbackFromHigh <= -1.5) {{
        label = "冲高回落但仍在均价上";
        level = "warn";
      }} else if (aboveAvg === false && pullbackFromHigh <= -1.5) {{
        label = "跌破均价，冲高回落";
        level = "danger";
      }} else if (aboveAvg === false) {{
        label = "均价线压制";
        level = "warn";
      }}
      return {{
        label,
        level,
        lastTime: last.time,
        lastPrice: price,
        avgPrice: avg || null,
        aboveAvg,
        moveFromOpen,
        pullbackFromHigh,
        high,
        low
      }};
    }}
    function currentAiPayload() {{
      const regime = marketRegime(latestMarketQuotes);
      return {{
        generatedAt: new Date().toISOString(),
        market: {{
          regime,
          sectorFlows: latestSectorFlows,
          retailSentiment: latestRetailSentiment,
          quotes: Object.values(latestMarketQuotes).map(q => ({{
            symbol: q.symbol,
            name: q.name,
            price: q.price,
            pct: q.pct,
            change: q.change
          }}))
        }},
        positions: positions.map(p => {{
          const q = latestQuotes[p.symbol] || null;
          const flow = latestFlows[p.symbol] || null;
          const pnlPct = q && Number(p.cost) > 0 ? q.price / p.cost - 1 : null;
          const flowEval = q ? flowScore(flow, q, regime) : null;
          const lines = q && flowEval ? dynamicLines(p, q, pnlPct, regime, flowEval) : null;
          const volEval = q ? volumeEval(q, latestVolumeProfiles[p.symbol]) : null;
          const support = q && lines ? supportEval(p, q, latestVolumeProfiles[p.symbol], lines) : null;
          const intraday = q ? intradayEvaluation(p, q) : null;
          return {{
            symbol: p.symbol,
            type: typeLabel(p),
            cost: p.cost,
            shares: p.shares,
            stop: p.stop,
            target: p.target,
            quote: q ? {{
              name: q.name,
              price: q.price,
              pct: q.pct,
              change: q.change,
              high: q.high,
              low: q.low,
              pe: q.pe,
              pb: q.pb,
              turnover: q.turnover,
              amount: q.amount
            }} : null,
            volumeEvaluation: volEval,
            support,
            intraday,
            flow: flow && flow.latest ? {{
              latest: flow.latest,
              trendMainNet: flow.trendMainNet,
              updatedAt: flow.updatedAt
            }} : null,
            localEvaluation: flowEval,
            dynamicLines: lines
          }};
        }})
      }};
    }}
    async function requestAiAnalysis() {{
      const button = document.getElementById("aiAnalyze");
      const status = document.getElementById("aiStatus");
      button.disabled = true;
      status.textContent = "DeepSeek 正在分析当前快照。";
      const snapshot = currentAiPayload();
      try {{
        const data = await requestDeepSeekAnalysis(snapshot);
        renderAiAnalysis(data.analysis || "AI 没有返回内容。");
        if (data.analysis) saveAiAnalysis(data.analysis);
        const source = data.provider === "qclaw-deepseek" ? "QClaw 兜底" : "DeepSeek";
        status.textContent = `${{source}} 分析完成 · ${{new Date().toLocaleTimeString("zh-CN", {{ hour12: false }})}}`;
      }} catch (error) {{
        const saved = loadAiAnalysis();
        status.textContent = saved ? `AI 分析失败，已保留上次结果 · ${{error.message}}` : "AI 分析失败。";
        if (!loadAiAnalysis()) {{
          renderAiAnalysis(`1. 盘面结论：本地 AI 服务未启动或 DeepSeek 请求失败。2. 风险提醒：${{error.message}}。3. 操作计划：先确认已启动 python -m quant_akshare.cli ai-server，并且本机已保存 DeepSeek API Key。`);
        }}
      }} finally {{
        button.disabled = false;
      }}
    }}
    async function requestDeepSeekAnalysis(snapshot) {{
      try {{
        const response = await fetch(AI_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify(snapshot)
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(data.error || `AI 服务返回 ${{response.status}}`);
        return data;
      }} catch (primaryError) {{
        const response = await fetch(QCLAW_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "analyze", payload: {{ snapshot }} }})
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok || data.type === "error") {{
          throw new Error(data.error || primaryError.message || `QClaw 服务返回 ${{response.status}}`);
        }}
        return {{ analysis: data.analysis, provider: "qclaw-deepseek" }};
      }}
    }}
    async function requestAiChat(question) {{
      const history = loadAiChat().map(item => ({{ role: item.role, content: item.content }}));
      const snapshot = currentAiPayload();
      try {{
        const response = await fetch(AI_CHAT_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ snapshot, question, history }})
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(data.error || `AI 对话服务返回 ${{response.status}}`);
        return data;
      }} catch (primaryError) {{
        const response = await fetch(QCLAW_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "chat", payload: {{ snapshot, question, history }} }})
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok || data.type === "error") {{
          throw new Error(data.error || primaryError.message || `QClaw 服务返回 ${{response.status}}`);
        }}
        return {{ answer: data.answer, provider: "qclaw-deepseek" }};
      }}
    }}
    async function submitAiChat(event) {{
      event.preventDefault();
      const input = document.getElementById("aiQuestion");
      const button = document.getElementById("aiSend");
      const status = document.getElementById("aiStatus");
      const question = input.value.trim();
      if (!question) return;
      appendAiChat("user", question);
      input.value = "";
      button.disabled = true;
      status.textContent = "AI 正在回答你的短问。";
      try {{
        const data = await requestAiChat(question);
        appendAiChat("assistant", replaceSymbolsWithNames(data.answer || "AI 没有返回内容。"));
        const source = data.provider === "qclaw-deepseek" ? "QClaw 兜底" : "DeepSeek";
        status.textContent = `${{source}} 对话完成 · ${{new Date().toLocaleTimeString("zh-CN", {{ hour12: false }})}}`;
      }} catch (error) {{
        appendAiChat("assistant", `AI 对话失败：${{error.message}}。先按看板里的支撑位、分时均价和止损线观察。`);
        status.textContent = "AI 对话失败，已保留问题。";
      }} finally {{
        button.disabled = false;
        input.focus();
      }}
    }}
    async function syncTradeToQClaw(trade) {{
      const response = await fetch(QCLAW_ENDPOINT, {{
        method: "POST",
        headers: {{ "Content-Type": "application/json" }},
        body: JSON.stringify({{ action: "apply_trade", payload: trade }})
      }});
      const data = await response.json().catch(() => ({{}}));
      if (!response.ok || data.type === "error") throw new Error(data.error || `服务返回 ${{response.status}}`);
      return data;
    }}
    function syncPositionsFromState(state, snapshotPositions = []) {{
      if (!state || !Array.isArray(state.holdings)) return;
      const previous = new Map(positions.map(item => [item.symbol, item]));
      const snapshotBySymbol = new Map((snapshotPositions || []).map(item => [String(item.symbol || ""), item]));
      const holdingSymbols = new Set();
      const next = state.holdings.map(raw => {{
        const symbol = String(raw.symbol || "");
        holdingSymbols.add(symbol);
        const base = previous.get(symbol) || defaultPositions.find(item => item.symbol === symbol) || {{ symbol, prefix: prefixForSymbol(symbol), secid: "", stop: null, target: null }};
        return {{ ...base, ...raw, ...(snapshotBySymbol.get(symbol) || {{}}), symbol, kind: "holding", prefix: base.prefix || prefixForSymbol(symbol) }};
      }});
      for (const rawSymbol of (state.watchlist || [])) {{
        const symbol = String(rawSymbol || "");
        if (!symbol || holdingSymbols.has(symbol)) continue;
        const base = previous.get(symbol) || defaultPositions.find(item => item.symbol === symbol) || {{ symbol, prefix: prefixForSymbol(symbol), secid: "", stop: null, target: null }};
        next.push({{ ...base, ...(snapshotBySymbol.get(symbol) || {{}}), symbol, cost: null, shares: null, kind: "watch", prefix: base.prefix || prefixForSymbol(symbol) }});
      }}
      const changed = positionKey(next) !== positionKey(positions);
      positions = next;
      savePositions();
      if (changed) {{
        renderTradeOptions();
        renderStockManagement();
        renderKlineShell();
        renderKlineCharts();
        renderIntradayShell();
        renderIntradayCharts();
        refreshIntradayProfiles();
      }}
    }}
    let snapshotSyncInFlight = false;
    function syncAccountSnapshotForm(account) {{
      latestAccountSnapshot = account || null;
      const form = document.getElementById("accountSnapshotForm");
      if (!form || form.contains(document.activeElement) || !latestAccountSnapshot) return;
      document.getElementById("accountTotalAssets").value = Number(latestAccountSnapshot.totalAssets || 0).toFixed(2);
      document.getElementById("accountMarketValue").value = Number(latestAccountSnapshot.marketValue || 0).toFixed(2);
      document.getElementById("accountDailyPnl").value = Number(latestAccountSnapshot.reportedDailyPnl || 0).toFixed(2);
      document.getElementById("accountNetTransfer").value = Number(latestAccountSnapshot.netTransfer || 0).toFixed(2);
      const message = document.getElementById("accountSnapshotMsg");
      message.textContent = `账户基准已保存 · ${{String(latestAccountSnapshot.capturedAt || "").replace("T", " ")}}`;
      message.className = "trade-msg up";
    }}
    async function saveAccountSnapshot(event) {{
      event.preventDefault();
      const button = document.getElementById("saveAccountSnapshot");
      const totalAssets = Number(document.getElementById("accountTotalAssets").value);
      const marketValue = Number(document.getElementById("accountMarketValue").value);
      const reportedDailyPnl = Number(document.getElementById("accountDailyPnl").value);
      const netTransfer = Number(document.getElementById("accountNetTransfer").value || 0);
      const message = document.getElementById("accountSnapshotMsg");
      if (!Number.isFinite(totalAssets) || totalAssets <= 0 || !Number.isFinite(marketValue) || marketValue < 0 || !Number.isFinite(reportedDailyPnl) || !Number.isFinite(netTransfer)) {{
        message.textContent = "请输入有效的总资产、总市值、当日盈亏和净转入。";
        message.className = "trade-msg down";
        return;
      }}
      button.disabled = true;
      message.textContent = "正在保存账户总资产基准。";
      message.className = "trade-msg neutral";
      try {{
        const data = await postQClawAction("update_account_snapshot", {{
          totalAssets,
          marketValue,
          reportedDailyPnl,
          netTransfer,
          tradeDate: currentTradeDate(),
          capturedAt: localDateTimeString()
        }});
        const snapshot = data.snapshot || {{}};
        latestPortfolioMetrics = snapshot.metrics || latestPortfolioMetrics;
        syncAccountSnapshotForm(snapshot.accountSnapshot || data.account_snapshot);
        renderTodayTrades(latestQuotes);
      }} catch (error) {{
        message.textContent = `账户基准保存失败：${{error.message}}`;
        message.className = "trade-msg down";
      }} finally {{
        button.disabled = false;
      }}
    }}
    async function syncDynamicSnapshot() {{
      if (snapshotSyncInFlight) return false;
      snapshotSyncInFlight = true;
      const status = document.getElementById("ledgerStatus");
      try {{
        const response = await fetch(QCLAW_SNAPSHOT_ENDPOINT, {{ cache: "no-store" }});
        const snapshot = await response.json().catch(() => ({{}}));
        if (!response.ok || snapshot.type === "error") throw new Error(snapshot.error || `服务返回 ${{response.status}}`);
        latestSnapshotTradeDate = snapshot.tradeDate || latestSnapshotTradeDate;
        dailyBaseline = Array.isArray(snapshot.dailyBaseline) ? snapshot.dailyBaseline : dailyBaseline;
        todayTrades = normalizeTodayTrades(snapshot.todayTrades || []);
        latestPortfolioMetrics = snapshot.metrics || null;
        syncAccountSnapshotForm(snapshot.accountSnapshot);
        latestLedgerRevision = snapshot.revision ?? latestLedgerRevision;
        if (snapshot.quotes && typeof snapshot.quotes === "object") {{
          latestQuotes = {{ ...latestQuotes, ...snapshot.quotes }};
        }}
        syncPositionsFromState(snapshot.state, snapshot.positions);
        const quoteState = snapshot.sourceStatus && snapshot.sourceStatus.quotes;
        status.textContent = quoteState === "ok" ? `账本已同步 · r${{latestLedgerRevision}}` : `账本已同步 · 行情${{quoteState === "partial" ? "部分缺失" : "不可用"}}`;
        status.className = `sync-status ${{quoteState === "ok" ? "ok" : "warn"}}`;
        render(latestQuotes, latestMarketQuotes);
        return true;
      }} catch (error) {{
        status.textContent = "账本离线";
        status.className = "sync-status down";
        return false;
      }} finally {{
        snapshotSyncInFlight = false;
      }}
    }}
    async function revokeTrade(id) {{
      if (!id || !window.confirm("撤回这笔本地交易记录，并反向还原看板持仓？不会向券商发送任何操作。")) return;
      try {{
        const response = await fetch(QCLAW_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "revoke_trade", payload: {{ id }} }})
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok || data.type === "error") throw new Error(data.error || `服务返回 ${{response.status}}`);
        syncPositionsFromState(data.state);
        await syncDynamicSnapshot();
        setTradeMessage(data.message || "已撤回本地交易记录。", "neutral");
        refresh();
      }} catch (error) {{
        setTradeMessage(`撤回失败：${{error.message}}`, "down");
      }}
    }}
    async function clearTodayTrades() {{
      if (!todayTrades.length) {{ setTradeMessage("今天没有可清空的交易记录。", "neutral"); return; }}
      if (!window.confirm("清空今天全部交易记录？此操作只删除日志，不会改变当前持仓。")) return;
      try {{
        const response = await fetch(QCLAW_ENDPOINT, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ action: "clear_today_trades", payload: {{}} }})
        }});
        const data = await response.json().catch(() => ({{}}));
        if (!response.ok || data.type === "error") throw new Error(data.error || `服务返回 ${{response.status}}`);
        await syncDynamicSnapshot();
        setTradeMessage(data.message || "已清空今天交易记录。", "neutral");
      }} catch (error) {{
        setTradeMessage(`清空失败：${{error.message}}`, "down");
      }}
    }}
    function render(quotes, marketQuotes) {{
      quotes = Object.keys(quotes || {{}}).length ? quotes : latestQuotes;
      const regime = marketRegime(marketQuotes);
      renderMarket(marketQuotes, regime);
      let totalValue = 0;
      let totalPnl = 0;
      let dangerCount = 0;
      let warnCount = 0;
      const rowHtml = [];
      const cardHtml = [];
      const flowEvals = [];
      for (const p of positions) {{
        const q = quotes[p.symbol];
        if (!q) continue;
        const pnlPct = Number(p.cost) > 0 ? q.price / p.cost - 1 : null;
        const pnl = p.cost !== null && p.cost !== undefined && p.shares ? (q.price - p.cost) * p.shares : null;
        const value = p.shares ? q.price * p.shares : null;
        const flowEval = flowScore(latestFlows[p.symbol], q, regime);
        const volEval = volumeEval(q, latestVolumeProfiles[p.symbol]);
        const lines = dynamicLines(p, q, pnlPct, regime, flowEval);
        const support = supportEval(p, q, latestVolumeProfiles[p.symbol], lines);
        if (value !== null) totalValue += value;
        if (pnl !== null) totalPnl += pnl;
        const [text, level] = advice(q, p, pnlPct, regime, flowEval);
        if (level === "danger") dangerCount += 1;
        if (level === "warn") warnCount += 1;
        flowEvals.push({{ p, q, flowEval, lines, text, level }});
        rowHtml.push(`
          <tr>
            <td>${{typeLabel(p)}}</td><td>${{p.symbol}}</td><td>${{q.name}}</td><td>${{priceNum(q.price, q)}}</td>
            <td class="${{cls(q.pct)}}">${{q.pct > 0 ? "+" : ""}}${{num(q.pct)}}%</td>
            <td class="volume-cell">${{renderVolumeMeter(volEval)}}</td>
            <td>${{priceNum(p.cost, q)}}</td><td>${{p.shares || "-"}}</td><td>${{money(value)}}</td>
            <td class="${{cls(pnl)}}">${{money(pnl, true)}} / ${{pnlPct === null ? "-" : (pnlPct > 0 ? "+" : "") + (pnlPct * 100).toFixed(2) + "%"}}</td>
            <td>${{priceNum(p.stop, q)}}</td><td>${{priceNum(support.value, q)}}<br><small>${{support.label}}</small></td><td>${{priceNum(p.target, q)}}</td><td>${{num(q.pe)}}</td><td>${{num(q.pb)}}</td>
            <td class="${{cls(flowEval.mainNet)}}">${{wan(flowEval.mainNet, true)}}<br><small>${{flowEval.updatedAt || ""}}</small></td>
            <td><span class="badge ${{flowEval.level}}">${{flowEval.label}} ${{flowEval.score}}</span></td>
            <td>${{priceNum(lines.stop, q)}} / ${{priceNum(lines.target, q)}}</td>
            <td><span class="badge ${{level}}">${{text}}</span></td>
          </tr>`);
        cardHtml.push(`
          <article class="stock-card">
            <h2>${{typeLabel(p)}} · ${{p.symbol}} ${{q.name}}</h2>
            <div class="grid">
              <div class="kv"><span>现价</span><strong>${{priceNum(q.price, q)}}</strong></div>
              <div class="kv"><span>涨跌幅</span><strong class="${{cls(q.pct)}}">${{q.pct > 0 ? "+" : ""}}${{num(q.pct)}}%</strong></div>
              <div class="kv"><span>量能</span><strong>${{volEval.label}} ${{volEval.ratio === null ? "-" : volEval.ratio.toFixed(2) + "x"}}</strong></div>
              <div class="kv"><span>浮盈亏</span><strong class="${{cls(pnl)}}">${{money(pnl, true)}}</strong></div>
              <div class="kv"><span>成本盈亏</span><strong class="${{cls(pnlPct)}}">${{pnlPct === null ? "-" : (pnlPct > 0 ? "+" : "") + (pnlPct * 100).toFixed(2) + "%"}}</strong></div>
              <div class="kv"><span>止损 / 目标</span><strong>${{priceNum(p.stop, q)}} / ${{priceNum(p.target, q)}}</strong></div>
              <div class="kv"><span>支撑位</span><strong>${{priceNum(support.value, q)}} · ${{support.label}}</strong></div>
              <div class="kv"><span>PE / PB</span><strong>${{num(q.pe)}} / ${{num(q.pb)}}</strong></div>
              <div class="kv"><span>东财主力净流入</span><strong class="${{cls(flowEval.mainNet)}}">${{wan(flowEval.mainNet, true)}}</strong></div>
              <div class="kv"><span>资金更新时间</span><strong>${{flowEval.updatedAt || "-"}}</strong></div>
              <div class="kv"><span>动态线</span><strong>${{priceNum(lines.stop, q)}} / ${{priceNum(lines.target, q)}}</strong></div>
            </div>
            <p><span class="badge ${{flowEval.level}}">${{flowEval.label}} ${{flowEval.score}}</span> <span class="badge ${{level}}">${{text}}</span></p>
          </article>`);
      }}
      document.getElementById("rows").innerHTML = rowHtml.join("");
      document.getElementById("cards").innerHTML = cardHtml.join("");
      document.getElementById("totalValue").textContent = money(totalValue);
      const totalPnlEl = document.getElementById("totalPnl");
      totalPnlEl.textContent = money(totalPnl, true);
      totalPnlEl.className = cls(totalPnl);
      document.getElementById("topAction").textContent = dangerCount ? "先处理风控" : (warnCount ? "降低风险观察" : (regime.state === "强势" ? "顺势持有" : "按计划持有"));
      document.getElementById("updatedAt").textContent = new Date().toLocaleString("zh-CN", {{ hour12: false }});
      renderTodayTrades(quotes);
      renderKlineCharts();
      renderIntradayCharts();
      renderAiDigest(regime, flowEvals);
    }}
    function parseFundFlowPayload(payload) {{
      const klines = payload && payload.data && Array.isArray(payload.data.klines) ? payload.data.klines : [];
      const points = klines.map(line => {{
        const parts = String(line).split(",");
        return {{
          time: parts[0],
          mainNet: Number(parts[1]) || 0,
          smallNet: Number(parts[2]) || 0,
          middleNet: Number(parts[3]) || 0,
          largeNet: Number(parts[4]) || 0,
          superLargeNet: Number(parts[5]) || 0
        }};
      }}).filter(point => point.time);
      const latest = points[points.length - 1] || null;
      const base = points[Math.max(0, points.length - 4)] || latest;
      return {{
        latest,
        points,
        trendMainNet: latest && base ? latest.mainNet - base.mainNet : 0,
        updatedAt: latest ? latest.time : ""
      }};
    }}
    function parseSectorFlowPayload(payload) {{
      const diff = payload && payload.data && Array.isArray(payload.data.diff) ? payload.data.diff : [];
      return diff.map(item => ({{
        code: String(item.f12 || ""),
        name: String(item.f14 || ""),
        pct: Number(item.f3) || 0,
        mainNet: Number(item.f62) || 0,
        mainNetRatio: Number(item.f184) || 0,
        updatedAt: item.f124 ? new Date(Number(item.f124) * 1000).toLocaleTimeString("zh-CN", {{ hour12: false }}) : ""
      }})).filter(item => item.name);
    }}
    function parseDailyKlinePayload(payload) {{
      const klines = payload && payload.data && Array.isArray(payload.data.klines) ? payload.data.klines : [];
      const rows = klines.map(line => {{
        const parts = String(line).split(",");
        return {{
          date: parts[0],
          open: Number(parts[1]) || 0,
          close: Number(parts[2]) || 0,
          high: Number(parts[3]) || 0,
          low: Number(parts[4]) || 0,
          amount: Number(parts[6]) || 0,
          pct: Number(parts[8]) || 0
        }};
      }}).filter(row => row.amount > 0);
      const baseline = rows.length > 1 ? rows.slice(0, -1) : rows;
      const sample = baseline.slice(-5);
      const avgAmount = sample.length ? sample.reduce((sum, row) => sum + row.amount, 0) / sample.length : null;
      const recentLow = sample.length ? Math.min(...sample.map(row => row.low).filter(Boolean)) : null;
      return {{ avgAmount, recentLow, sampleCount: sample.length, rows: rows.slice(-30) }};
    }}
    function parseIntradayPayload(payload) {{
      const trends = payload && payload.data && Array.isArray(payload.data.trends) ? payload.data.trends : [];
      const rows = trends.map(line => {{
        const parts = String(line).split(",");
        return {{
          time: parts[0],
          price: Number(parts[2]) || 0,
          high: Number(parts[3]) || 0,
          low: Number(parts[4]) || 0,
          volume: Number(parts[5]) || 0,
          amount: Number(parts[6]) || 0,
          avg: Number(parts[7]) || 0
        }};
      }}).filter(row => row.time && row.price > 0);
      return {{ rows }};
    }}
    function recentKlineBegDate() {{
      const date = new Date();
      date.setDate(date.getDate() - 90);
      const year = date.getFullYear();
      const month = String(date.getMonth() + 1).padStart(2, "0");
      const day = String(date.getDate()).padStart(2, "0");
      return `${{year}}${{month}}${{day}}`;
    }}
    function fetchFundFlow(p) {{
      return new Promise(resolve => {{
        const callbackName = `fundFlow_${{p.symbol}}_${{Date.now()}}_${{Math.floor(Math.random() * 10000)}}`;
        const script = document.createElement("script");
        const cleanup = () => {{
          delete window[callbackName];
          script.remove();
        }};
        const timer = setTimeout(() => {{
          cleanup();
          resolve([p.symbol, null]);
        }}, 7000);
        window[callbackName] = payload => {{
          clearTimeout(timer);
          const parsed = parseFundFlowPayload(payload);
          cleanup();
          resolve([p.symbol, parsed]);
        }};
        script.src = `https://push2.eastmoney.com/api/qt/stock/fflow/kline/get?cb=${{callbackName}}&lmt=20&klt=1&secid=${{p.secid || ""}}&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63&_=${{Date.now()}}`;
        script.onerror = () => {{
          clearTimeout(timer);
          cleanup();
          resolve([p.symbol, null]);
        }};
        document.body.appendChild(script);
      }});
    }}
    async function refreshFundFlows() {{
      const pairs = await Promise.all(positions.map(fetchFundFlow));
      for (const [symbol, flow] of pairs) {{
        if (flow) latestFlows[symbol] = flow;
      }}
      render(latestQuotes, latestMarketQuotes);
    }}
    function fetchSectorFlowPage(direction) {{
      return new Promise(resolve => {{
        const old = document.getElementById(`sectorFlowScript-${{direction}}`);
        if (old) old.remove();
        const callbackName = `sectorFlow_${{direction}}_${{Date.now()}}_${{Math.floor(Math.random() * 10000)}}`;
        const script = document.createElement("script");
        const cleanup = () => {{
          delete window[callbackName];
          script.remove();
        }};
        const timer = setTimeout(() => {{
          cleanup();
          resolve([]);
        }}, 7000);
        window[callbackName] = payload => {{
          clearTimeout(timer);
          const rows = parseSectorFlowPayload(payload);
          cleanup();
          resolve(rows);
        }};
        script.id = `sectorFlowScript-${{direction}}`;
        script.src = `https://push2.eastmoney.com/api/qt/clist/get?cb=${{callbackName}}&fid=f62&po=${{direction}}&pz=60&pn=1&np=1&fltt=2&invt=2&fs=m:90+t:2&fields=f12,f14,f3,f62,f66,f69,f72,f75,f78,f81,f84,f87,f124,f184&_=${{Date.now()}}`;
        script.onerror = () => {{
          clearTimeout(timer);
          cleanup();
          resolve([]);
        }};
        document.body.appendChild(script);
      }});
    }}
    async function refreshSectorFlows() {{
      const [inflowRows, outflowRows] = await Promise.all([
        fetchSectorFlowPage(1),
        fetchSectorFlowPage(0)
      ]);
      if (!inflowRows.length && !outflowRows.length) {{
        renderSectorFlows(latestSectorFlows);
        return;
      }}
      const seen = new Set();
      latestSectorFlows = inflowRows.concat(outflowRows).filter(item => {{
        const key = item.code || item.name;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      }});
      renderSectorFlows(latestSectorFlows);
    }}
    function fetchVolumeProfile(p) {{
      return new Promise(resolve => {{
        const callbackName = `dailyKline_${{p.symbol}}_${{Date.now()}}_${{Math.floor(Math.random() * 10000)}}`;
        const script = document.createElement("script");
        const cleanup = () => {{
          delete window[callbackName];
          script.remove();
        }};
        const timer = setTimeout(() => {{
          cleanup();
          resolve([p.symbol, null]);
        }}, 7000);
        window[callbackName] = payload => {{
          clearTimeout(timer);
          const parsed = parseDailyKlinePayload(payload);
          cleanup();
          resolve([p.symbol, parsed]);
        }};
        script.src = `https://push2his.eastmoney.com/api/qt/stock/kline/get?cb=${{callbackName}}&secid=${{p.secid || ""}}&klt=101&fqt=1&lmt=30&beg=${{recentKlineBegDate()}}&end=20500101&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&_=${{Date.now()}}`;
        script.onerror = () => {{
          clearTimeout(timer);
          cleanup();
          resolve([p.symbol, null]);
        }};
        document.body.appendChild(script);
      }});
    }}
    async function refreshVolumeProfiles() {{
      const pairs = await Promise.all(positions.map(fetchVolumeProfile));
      for (const [symbol, profile] of pairs) {{
        if (profile && (profile.avgAmount || (profile.rows && profile.rows.length))) latestVolumeProfiles[symbol] = profile;
      }}
      render(latestQuotes, latestMarketQuotes);
      renderKlineCharts();
    }}
    function fetchIntradayProfile(p) {{
      return new Promise(resolve => {{
        const callbackName = `intraday_${{p.symbol}}_${{Date.now()}}_${{Math.floor(Math.random() * 10000)}}`;
        const script = document.createElement("script");
        const cleanup = () => {{
          delete window[callbackName];
          script.remove();
        }};
        const timer = setTimeout(() => {{
          cleanup();
          resolve([p.symbol, null]);
        }}, 7000);
        window[callbackName] = payload => {{
          clearTimeout(timer);
          const parsed = parseIntradayPayload(payload);
          cleanup();
          resolve([p.symbol, parsed]);
        }};
        script.src = `https://push2his.eastmoney.com/api/qt/stock/trends2/get?cb=${{callbackName}}&secid=${{p.secid || ""}}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13&fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&iscca=0&ndays=1&_=${{Date.now()}}`;
        script.onerror = () => {{
          clearTimeout(timer);
          cleanup();
          resolve([p.symbol, null]);
        }};
        document.body.appendChild(script);
      }});
    }}
    async function refreshIntradayProfiles() {{
      const pairs = await Promise.all(positions.map(fetchIntradayProfile));
      for (const [symbol, profile] of pairs) {{
        if (profile && profile.rows && profile.rows.length) latestIntradayProfiles[symbol] = profile;
      }}
      renderIntradayCharts();
    }}
    function refresh() {{
      const old = document.getElementById("quoteScript");
      if (old) old.remove();
      const names = positions.map(p => `${{p.prefix}}${{p.symbol}}`).concat(marketSymbols.map(m => m.symbol));
      const script = document.createElement("script");
      script.id = "quoteScript";
      script.charset = "gbk";
      script.src = `https://qt.gtimg.cn/q=${{names.join(",")}}&_=${{Date.now()}}`;
      script.onload = () => {{
        const quotes = {{}};
        const marketQuotes = {{}};
        for (const p of positions) {{
          const raw = window[`v_${{p.prefix}}${{p.symbol}}`];
          const parsed = parseTencentLine(`v_${{p.prefix}}${{p.symbol}}="${{raw}}"`);
          if (parsed) quotes[p.symbol] = parsed;
        }}
        for (const m of marketSymbols) {{
          const raw = window[`v_${{m.symbol}}`];
          const parsed = parseTencentLine(`v_${{m.symbol}}="${{raw}}"`);
          if (parsed) marketQuotes[m.symbol] = parsed;
        }}
        if (Object.keys(quotes).length) {{
          latestQuotes = quotes;
        }}
        if (Object.keys(marketQuotes).length) {{
          latestMarketQuotes = marketQuotes;
        }}
        render(latestQuotes, latestMarketQuotes);
      }};
      script.onerror = () => {{
        setTradeMessage("行情刷新失败，已保留上一帧数据。", "down");
        render(latestQuotes, latestMarketQuotes);
      }};
      document.body.appendChild(script);
    }}
    async function applyTrade(event) {{
      event.preventDefault();
      const submitButton = event.submitter || document.querySelector("#tradeForm button[type=submit]");
      const symbol = document.getElementById("tradeSymbol").value;
      const side = document.getElementById("tradeSide").value;
      const price = Number(document.getElementById("tradePrice").value);
      const shares = Number(document.getElementById("tradeShares").value);
      if (!symbol || !price || !shares || price <= 0 || shares <= 0) {{
        setTradeMessage("请输入有效的成交价和股数。", "down");
        return;
      }}
      const index = positions.findIndex(p => p.symbol === symbol);
      if (index < 0) {{
        setTradeMessage("未找到这只股票。", "down");
        return;
      }}
      const current = positions[index];
      const currentShares = Number(current.shares || 0);
      if (side === "sell" && shares > currentShares) {{
        setTradeMessage(`卖出股数 ${{shares}} 超过当前持仓 ${{currentShares}}。`, "down");
        return;
      }}
      const tradeRecord = {{ symbol, side, price, shares, id: newTradeId(), time: localDateTimeString() }};
      submitButton.disabled = true;
      setTradeMessage("正在写入组合账本。", "neutral");
      try {{
        const data = await syncTradeToQClaw(tradeRecord);
        syncPositionsFromState(data.state);
        await syncDynamicSnapshot();
        document.getElementById("tradePrice").value = "";
        document.getElementById("tradeShares").value = "";
        setTradeMessage(`${{displayNameForSymbol(symbol)}} ${{side === "buy" ? "买入" : "卖出"}} ${{shares}} 股已写入账本。`, side === "buy" ? "up" : "neutral");
        refresh();
      }} catch (error) {{
        setTradeMessage(`记录失败：${{error.message}}。持仓和交易日志均未修改。`, "down");
      }} finally {{
        submitButton.disabled = false;
      }}
    }}
    document.getElementById("tradeForm").addEventListener("submit", applyTrade);
    document.getElementById("accountSnapshotForm").addEventListener("submit", saveAccountSnapshot);
    document.getElementById("addStockForm").addEventListener("submit", addStock);
    document.getElementById("removeWatchForm").addEventListener("submit", removeWatch);
    document.getElementById("newStockType").addEventListener("change", updateNewStockFields);
    document.getElementById("resetPositions").addEventListener("click", resetPositions);
    document.getElementById("clearTodayTrades").addEventListener("click", clearTodayTrades);
    document.getElementById("aiAnalyze").addEventListener("click", requestAiAnalysis);
    document.getElementById("aiChatForm").addEventListener("submit", submitAiChat);
    renderTradeOptions();
    renderStockManagement();
    updateNewStockFields();
    syncAccountSnapshotForm(latestAccountSnapshot);
    renderAiChat();
    renderKlineShell();
    renderIntradayShell();
    renderSectorFlows(latestSectorFlows);
    renderRetailSentiment(latestRetailSentiment);
    if (dashboardTradeDayIsCurrent()) {{
      syncDynamicSnapshot();
      refresh();
      refreshFundFlows();
      refreshVolumeProfiles();
      refreshIntradayProfiles();
      refreshSectorFlows();
      setInterval(refresh, DASHBOARD.refreshSeconds * 1000);
      setInterval(refreshFundFlows, FLOW_REFRESH_SECONDS * 1000);
      setInterval(refreshVolumeProfiles, 5 * 60 * 1000);
      setInterval(refreshIntradayProfiles, 30 * 1000);
      setInterval(refreshSectorFlows, FLOW_REFRESH_SECONDS * 1000);
      setInterval(syncDynamicSnapshot, DASHBOARD.refreshSeconds * 1000);
    }} else {{
      refreshForNewTradeDay();
    }}
  </script>
</body>
</html>
"""


def _initial_row(view: PositionView) -> str:
    quote = view.quote
    item = view.item
    volume_label, volume_level = _turnover_volume_label(quote.turnover)
    support_value, support_label = _initial_support(quote, item)
    return f"""
        <tr>
          <td>{'自选' if not item.shares and not item.cost else '持仓'}</td><td>{escape(quote.symbol)}</td><td>{escape(quote.name)}</td><td>{_price_fmt(quote.price, quote.symbol, quote.name)}</td>
          <td class="{_num_class(quote.pct_change)}">{quote.pct_change:+.2f}%</td><td class="volume-cell"><span class="badge {volume_level}">{escape(volume_label)}</span></td>
          <td>{_price_fmt(item.cost, quote.symbol, quote.name)}</td><td>{_fmt(item.shares, 0)}</td><td>{_money(view.market_value)}</td>
          <td class="{_num_class(view.pnl_amount)}">{_money(view.pnl_amount, signed=True)} / {_pct(view.pnl_pct)}</td>
          <td>{_price_fmt(_first_below(item), quote.symbol, quote.name)}</td><td>{_price_fmt(support_value, quote.symbol, quote.name)}<br><small>{escape(support_label)}</small></td><td>{_price_fmt(_first_above(item), quote.symbol, quote.name)}</td><td>{_fmt(quote.pe)}</td><td>{_fmt(quote.pb)}</td>
          <td>读取中</td><td><span class="badge neutral">资金读取中</span></td><td>- / -</td>
          <td><span class="badge {view.advice_level}">{escape(view.advice)}</span></td>
        </tr>"""


def _initial_card(view: PositionView) -> str:
    quote = view.quote
    item = view.item
    volume_label, _volume_level = _turnover_volume_label(quote.turnover)
    support_value, support_label = _initial_support(quote, item)
    return f"""
      <article class="stock-card">
        <h2>{escape(quote.symbol)} {escape(quote.name)}</h2>
        <div class="grid">
          <div class="kv"><span>现价</span><strong>{_price_fmt(quote.price, quote.symbol, quote.name)}</strong></div>
          <div class="kv"><span>涨跌幅</span><strong class="{_num_class(quote.pct_change)}">{quote.pct_change:+.2f}%</strong></div>
          <div class="kv"><span>量能</span><strong>{escape(volume_label)}</strong></div>
          <div class="kv"><span>浮盈亏</span><strong class="{_num_class(view.pnl_amount)}">{_money(view.pnl_amount, signed=True)}</strong></div>
          <div class="kv"><span>成本盈亏</span><strong class="{_num_class(view.pnl_pct)}">{_pct(view.pnl_pct)}</strong></div>
          <div class="kv"><span>止损 / 目标</span><strong>{_price_fmt(_first_below(item), quote.symbol, quote.name)} / {_price_fmt(_first_above(item), quote.symbol, quote.name)}</strong></div>
          <div class="kv"><span>支撑位</span><strong>{_price_fmt(support_value, quote.symbol, quote.name)} · {escape(support_label)}</strong></div>
          <div class="kv"><span>PE / PB</span><strong>{_fmt(quote.pe)} / {_fmt(quote.pb)}</strong></div>
          <div class="kv"><span>东财主力净流入</span><strong>读取中</strong></div>
          <div class="kv"><span>动态线</span><strong>- / -</strong></div>
        </div>
        <p><span class="badge {view.advice_level}">{escape(view.advice)}</span></p>
      </article>"""


def _first_below(item: WatchItem) -> float | None:
    return next((rule.below for rule in item.rules if rule.below is not None), None)


def _first_above(item: WatchItem) -> float | None:
    return next((rule.above for rule in item.rules if rule.above is not None), None)


def _initial_support(quote: SpotQuote, item: WatchItem) -> tuple[float | None, str]:
    candidates: list[tuple[float, str]] = []
    stop = _first_below(item)
    if stop:
        candidates.append((stop, "止损线"))
    if item.cost and item.cost <= quote.price * 1.015:
        candidates.append((item.cost, "成本附近"))
    if quote.low:
        candidates.append((quote.low, "日内低点"))
    valid = [(value, label) for value, label in candidates if value > 0 and value <= quote.price * 1.02]
    if not valid:
        return None, "暂无"
    value, label = sorted(valid, key=lambda item: item[0], reverse=True)[0]
    return value, label


def _turnover_volume_label(turnover: float) -> tuple[str, str]:
    if turnover >= 5:
        return "高换手放量", "good"
    if turnover >= 3:
        return "温和放量", "good"
    if turnover <= 0.6:
        return "低换手缩量", "danger"
    if turnover <= 1.2:
        return "略缩量", "warn"
    return "平量观察", "neutral"


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _price_fmt(value: float | None, symbol: str = "", name: str = "") -> str:
    if value is None:
        return "-"
    if _is_etf(symbol, name):
        return f"{value:.3f}"
    third_digit = round(value * 1000) - round(value * 100) * 10
    digits = 3 if third_digit else 2
    return f"{value:.{digits}f}"


def _is_etf(symbol: str = "", name: str = "") -> bool:
    symbol = str(symbol or "")
    name = str(name or "")
    return "ETF" in name or "基金" in name or symbol.startswith(("15", "16", "18", "50", "51", "56", "58"))


def _pct(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:+.2%}"


def _money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    prefix = "+" if signed and value > 0 else ""
    return prefix + f"{value:,.0f}"


def _num_class(value: float | None) -> str:
    if value is None:
        return "neutral"
    if value > 0:
        return "up"
    if value < 0:
        return "down"
    return "neutral"


def _top_action(views: list[PositionView]) -> str:
    if any(view.advice_level == "danger" for view in views):
        return "先处理风控"
    if any(view.advice_level == "warn" for view in views):
        return "降低风险观察"
    return "按计划持有"
