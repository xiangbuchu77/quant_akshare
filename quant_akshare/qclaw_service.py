from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from math import isfinite
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import requests

from .ai_server import DEFAULT_DEEPSEEK_MODEL, analyze_with_deepseek, chat_with_deepseek, load_deepseek_api_key
from .dashboard import DAILY_SNAPSHOT_PATH, render_dashboard
from .portfolio_book import PortfolioBookStore
from .realtime import AlertRule, WatchItem, fetch_spot_quotes
from .realtime import SpotQuote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = PROJECT_ROOT / "data" / "qclaw_portfolio.json"
REPORT_PATH = PROJECT_ROOT / "reports" / "portfolio_dashboard.html"
ANALYSIS_PATH = PROJECT_ROOT / "reports" / "latest_ai_analysis.txt"
TRADE_LOG_PATH = PROJECT_ROOT / "data" / "qclaw_trades.jsonl"
BOOK_PATH = PROJECT_ROOT / "data" / "portfolio_book.json"


@dataclass
class QClawState:
    holdings: list[dict[str, Any]]
    watchlist: list[str]
    order: list[str] | None = None
    line_overrides: dict[str, dict[str, float]] | None = None


def default_state() -> QClawState:
    return QClawState(
        holdings=[],
        watchlist=[],
        order=[],
        line_overrides={},
    )


def _book_store() -> PortfolioBookStore:
    data_dir = PROJECT_ROOT / "data"
    if STATE_PATH.parent == data_dir:
        return PortfolioBookStore(BOOK_PATH, STATE_PATH, TRADE_LOG_PATH, DAILY_SNAPSHOT_PATH)
    isolated_dir = STATE_PATH.parent
    return PortfolioBookStore(
        isolated_dir / "portfolio_book.json",
        STATE_PATH,
        isolated_dir / TRADE_LOG_PATH.name,
        isolated_dir / DAILY_SNAPSHOT_PATH.name,
    )


def _state_from_dict(data: dict[str, Any]) -> QClawState:
    return QClawState(
        holdings=list(data.get("holdings") or []),
        watchlist=[str(symbol) for symbol in data.get("watchlist") or []],
        order=[str(symbol) for symbol in data.get("order") or []],
        line_overrides=dict(data.get("line_overrides") or {}),
    )


def _default_state_dict() -> dict[str, Any]:
    return asdict(default_state())


def read_state() -> QClawState:
    book = _book_store().load(_default_state_dict())
    return _state_from_dict(book["state"])


def write_state(state: QClawState) -> None:
    def mutate(book: dict[str, Any]) -> None:
        book["state"] = asdict(state)

    _book_store().update(_default_state_dict(), mutate)


def ensure_daily_position_snapshot(state: QClawState, today: str | None = None) -> list[dict[str, Any]]:
    """Freeze the first known holding quantities for a trading day.

    Daily P&L values later use these quantities with each quote's previous close.
    """
    date_key = today or datetime.now().date().isoformat()
    store = _book_store()
    book = store.load(_default_state_dict())
    snapshots = dict(book.get("snapshots") or {})
    existing = snapshots.get(date_key)
    if isinstance(existing, dict) and isinstance(existing.get("positions"), list):
        return existing["positions"]
    # If the dashboard is first opened after a trade, reverse today's ledger to
    # recover the prior-close quantities rather than freezing an intraday state.
    opening_shares = {
        str(item.get("symbol")): _optional_float(item.get("shares")) or 0.0
        for item in state.holdings
        if item.get("symbol")
    }
    for record in reversed(_read_trade_records()):
        if not str(record.get("time") or "").startswith(date_key):
            continue
        symbol = str(record.get("symbol") or "").strip()
        shares = _optional_float(record.get("shares")) or 0.0
        side = _normalise_trade_side(record.get("side"))
        if not symbol or shares <= 0:
            continue
        current_shares = opening_shares.get(symbol, 0.0)
        opening_shares[symbol] = current_shares - shares if side == "buy" else current_shares + shares
    positions = [
        {"symbol": symbol, "shares": shares}
        for symbol, shares in opening_shares.items()
        if shares > 0
    ]
    snapshots[date_key] = {"capturedAt": datetime.now().isoformat(timespec="seconds"), "positions": positions}
    def mutate(updated_book: dict[str, Any]) -> None:
        updated_snapshots = dict(updated_book.get("snapshots") or {})
        updated_snapshots[date_key] = snapshots[date_key]
        updated_book["snapshots"] = updated_snapshots

    store.update(_default_state_dict(), mutate)
    return positions


def update_account_snapshot(
    total_assets: float,
    market_value: float,
    reported_daily_pnl: float,
    net_transfer: float = 0.0,
    trade_date: str | None = None,
    captured_at: str | None = None,
) -> dict[str, Any]:
    """Save a broker account anchor for account-level intraday P&L."""
    values = (total_assets, market_value, reported_daily_pnl, net_transfer)
    if not all(isfinite(float(value)) for value in values):
        raise ValueError("account snapshot values must be finite")
    if total_assets <= 0 or market_value < 0:
        raise ValueError("total assets must be positive and market value cannot be negative")

    date_key = trade_date or datetime.now().date().isoformat()
    captured = captured_at or datetime.now().isoformat(timespec="seconds")
    opening_assets = total_assets - net_transfer - reported_daily_pnl
    if opening_assets <= 0:
        raise ValueError("derived previous-close assets must be positive")
    account = {
        "tradeDate": date_key,
        "capturedAt": captured,
        "totalAssets": round(float(total_assets), 3),
        "marketValue": round(float(market_value), 3),
        "cashComponent": round(float(total_assets - market_value), 3),
        "openingAssets": round(float(opening_assets), 3),
        "netTransfer": round(float(net_transfer), 3),
        "reportedDailyPnl": round(float(reported_daily_pnl), 3),
        "source": "broker-manual",
    }

    def mutate(book: dict[str, Any]) -> None:
        snapshots = dict(book.get("snapshots") or {})
        day_snapshot = dict(snapshots.get(date_key) or {})
        day_snapshot["account"] = account
        snapshots[date_key] = day_snapshot
        book["snapshots"] = snapshots

    _book_store().update(_default_state_dict(), mutate)
    return account


def merge_holdings(state: QClawState, holdings: list[dict[str, Any]], persist: bool = True) -> QClawState:
    by_symbol = {str(item.get("symbol")): dict(item) for item in state.holdings if item.get("symbol")}
    for raw in holdings:
        symbol = str(raw.get("symbol") or "").strip()
        if not _valid_symbol(symbol):
            raise ValueError(f"invalid stock symbol: {symbol or 'missing symbol'}")
        by_symbol[symbol] = {
            "symbol": symbol,
            "cost": _optional_float(raw.get("cost")),
            "shares": _optional_float(raw.get("shares")),
            "stop": _optional_float(raw.get("stop")),
            "target": _optional_float(raw.get("target")),
        }
    state.holdings = list(by_symbol.values())
    holding_symbols = {item["symbol"] for item in state.holdings}
    state.watchlist = [symbol for symbol in state.watchlist if symbol not in holding_symbols]
    _ensure_order(state)
    if persist:
        write_state(state)
    return state


def replace_holdings_snapshot(
    state: QClawState,
    holdings: list[dict[str, Any]],
    persist: bool = True,
    reset_daily_baseline: bool = True,
) -> QClawState:
    normalized: list[dict[str, Any]] = []
    for raw in holdings:
        symbol = str(raw.get("symbol") or "").strip()
        cost = _optional_float(raw.get("cost"))
        shares = _optional_float(raw.get("shares"))
        if not _valid_symbol(symbol) or cost is None or shares is None or shares <= 0:
            raise ValueError(f"invalid holding snapshot: {symbol or 'missing symbol'}")
        normalized.append({"symbol": symbol, "cost": cost, "shares": shares})

    old_holding_symbols = [
        str(item.get("symbol"))
        for item in state.holdings
        if item.get("symbol")
    ]
    new_symbols = [item["symbol"] for item in normalized]
    watchlist = list(state.watchlist)
    for symbol in old_holding_symbols:
        if symbol not in new_symbols and symbol not in watchlist:
            watchlist.append(symbol)
    state.holdings = normalized
    state.watchlist = [symbol for symbol in watchlist if symbol not in set(new_symbols)]
    state.order = new_symbols + [
        symbol
        for symbol in _ordered_symbols(state)
        if symbol not in new_symbols
    ]
    state.line_overrides = {
        symbol: lines
        for symbol, lines in dict(state.line_overrides or {}).items()
        if symbol in set(state.order)
    }
    if not persist:
        return state

    def mutate(book: dict[str, Any]) -> None:
        book["state"] = asdict(state)
        if reset_daily_baseline:
            date_key = datetime.now().date().isoformat()
            snapshots = dict(book.get("snapshots") or {})
            snapshots[date_key] = {
                "capturedAt": datetime.now().isoformat(timespec="seconds"),
                "positions": [
                    {"symbol": item["symbol"], "shares": item["shares"]}
                    for item in normalized
                ],
            }
            book["snapshots"] = snapshots

    _book_store().update(_default_state_dict(), mutate)
    return state


def merge_holding_trades(state: QClawState, holdings: list[dict[str, Any]], persist: bool = True) -> QClawState:
    if persist:
        result: dict[str, QClawState] = {}

        def mutate(book: dict[str, Any]) -> None:
            current_state = _state_from_dict(book["state"])
            records = list(book.get("trades") or [])
            for raw in holdings:
                symbol = str(raw.get("symbol") or "").strip()
                price = _optional_float(raw.get("price")) or _optional_float(raw.get("cost")) or 0.0
                shares = _optional_float(raw.get("shares")) or 0.0
                side = _normalise_trade_side(raw.get("side") or raw.get("direction") or "buy")
                current_holding = next(
                    (item for item in current_state.holdings if str(item.get("symbol")) == symbol),
                    {},
                )
                cost_basis = _optional_float(current_holding.get("cost")) or price
                current_state = apply_trade(
                    current_state, symbol=symbol, side=side, price=price, shares=shares, persist=False
                )
                records.append(
                    _new_trade_record(
                        symbol=symbol,
                        side=side,
                        price=price,
                        shares=shares,
                        cost_basis=cost_basis,
                    )
                )
            book["state"] = asdict(current_state)
            book["trades"] = records
            result["state"] = current_state

        _book_store().update(_default_state_dict(), mutate)
        return result["state"]

    for raw in holdings:
        symbol = str(raw.get("symbol") or "").strip()
        price = _optional_float(raw.get("price")) or _optional_float(raw.get("cost")) or 0.0
        shares = _optional_float(raw.get("shares")) or 0.0
        side = str(raw.get("side") or raw.get("direction") or "buy")
        state = apply_trade(state, symbol=symbol, side=side, price=price, shares=shares, persist=False)
    return state


def merge_watchlist(state: QClawState, symbols: list[str], persist: bool = True) -> QClawState:
    holding_symbols = {str(item.get("symbol")) for item in state.holdings}
    merged = list(state.watchlist)
    for symbol in symbols:
        symbol = str(symbol).strip()
        if not _valid_symbol(symbol):
            raise ValueError(f"invalid stock symbol: {symbol or 'missing symbol'}")
        if symbol and symbol not in holding_symbols and symbol not in merged:
            merged.append(symbol)
    state.watchlist = merged
    _ensure_order(state)
    if persist:
        write_state(state)
    return state


def remove_holdings(state: QClawState, symbols: list[str], persist: bool = True) -> QClawState:
    remove_set = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
    state.holdings = [item for item in state.holdings if str(item.get("symbol")) not in remove_set]
    _remove_from_order_and_overrides(state, remove_set)
    if persist:
        write_state(state)
    return state


def remove_watchlist(state: QClawState, symbols: list[str], persist: bool = True) -> QClawState:
    remove_set = {str(symbol).strip() for symbol in symbols if str(symbol).strip()}
    state.watchlist = [symbol for symbol in state.watchlist if symbol not in remove_set]
    _remove_from_order_and_overrides(state, remove_set)
    if persist:
        write_state(state)
    return state


def clear_watchlist(state: QClawState, persist: bool = True) -> QClawState:
    state.watchlist = []
    current_holdings = {str(item.get("symbol")) for item in state.holdings}
    state.order = [symbol for symbol in (state.order or []) if symbol in current_holdings]
    if persist:
        write_state(state)
    return state


def update_risk_lines(state: QClawState, lines: list[dict[str, Any]], persist: bool = True) -> QClawState:
    state.line_overrides = dict(state.line_overrides or {})
    for raw in lines:
        symbol = str(raw.get("symbol") or "").strip()
        if not symbol:
            continue
        line: dict[str, float] = {}
        stop = _optional_float(raw.get("stop"))
        target = _optional_float(raw.get("target"))
        if stop is not None:
            line["stop"] = stop
        if target is not None:
            line["target"] = target
        if line:
            state.line_overrides[symbol] = line
    if persist:
        write_state(state)
    return state


def clear_risk_lines(state: QClawState, symbols: list[str] | None = None, persist: bool = True) -> QClawState:
    state.line_overrides = dict(state.line_overrides or {})
    if symbols:
        for symbol in symbols:
            state.line_overrides.pop(str(symbol).strip(), None)
    else:
        state.line_overrides = {}
    if persist:
        write_state(state)
    return state


def reorder_symbols(state: QClawState, symbols: list[str], persist: bool = True) -> QClawState:
    known = _known_symbols(state)
    requested = [str(symbol).strip() for symbol in symbols if str(symbol).strip() in known]
    remaining = [symbol for symbol in _ordered_symbols(state) if symbol not in requested]
    state.order = requested + remaining
    if persist:
        write_state(state)
    return state


def apply_trade(
    state: QClawState,
    symbol: str,
    side: str,
    price: float,
    shares: float,
    persist: bool = True,
    trade_id: str | None = None,
    trade_time: str | None = None,
) -> QClawState:
    symbol = str(symbol).strip()
    side = _normalise_trade_side(side)
    if not _valid_symbol(symbol) or price <= 0 or shares <= 0:
        raise ValueError("invalid trade")
    if persist:
        result: dict[str, QClawState] = {}

        def mutate(book: dict[str, Any]) -> None:
            current_state = _state_from_dict(book["state"])
            current_holding = next(
                (item for item in current_state.holdings if str(item.get("symbol")) == symbol),
                {},
            )
            cost_basis = _optional_float(current_holding.get("cost")) or price
            current_state = apply_trade(
                current_state,
                symbol=symbol,
                side=side,
                price=price,
                shares=shares,
                persist=False,
            )
            records = list(book.get("trades") or [])
            records.append(
                _new_trade_record(
                    symbol=symbol,
                    side=side,
                    price=price,
                    shares=shares,
                    record_id=trade_id,
                    trade_time=trade_time,
                    cost_basis=cost_basis,
                )
            )
            book["state"] = asdict(current_state)
            book["trades"] = records
            result["state"] = current_state

        _book_store().update(_default_state_dict(), mutate)
        return result["state"]

    holdings = {str(item.get("symbol")): dict(item) for item in state.holdings if item.get("symbol")}
    current = holdings.get(symbol, {"symbol": symbol, "cost": price, "shares": 0})
    current_shares = _optional_float(current.get("shares")) or 0.0
    current_cost = _optional_float(current.get("cost")) or price
    if side == "buy":
        new_shares = current_shares + shares
        current["cost"] = ((current_cost * current_shares) + (price * shares)) / new_shares
        current["shares"] = new_shares
        holdings[symbol] = current
        state.watchlist = [item for item in state.watchlist if item != symbol]
    elif side == "sell":
        if shares > current_shares:
            raise ValueError("sell shares exceed current holding")
        current["shares"] = current_shares - shares
        if current["shares"] > 0:
            holdings[symbol] = current
        else:
            holdings.pop(symbol, None)
            if symbol not in state.watchlist:
                state.watchlist.append(symbol)
    else:
        raise ValueError("side must be buy or sell")
    state.holdings = list(holdings.values())
    _ensure_order(state)
    return state


def _new_trade_record(
    symbol: str,
    side: str,
    price: float,
    shares: float,
    record_id: str | None = None,
    trade_time: str | None = None,
    cost_basis: float | None = None,
) -> dict[str, Any]:
    record = {
        "id": record_id or uuid4().hex,
        "time": trade_time or datetime.now().isoformat(timespec="seconds"),
        "symbol": symbol,
        "side": side,
        "price": price,
        "shares": shares,
    }
    if cost_basis is not None:
        record["cost_basis"] = cost_basis
    return record


def append_trade_log(
    symbol: str,
    side: str,
    price: float,
    shares: float,
    record_id: str | None = None,
    trade_time: str | None = None,
    cost_basis: float | None = None,
) -> dict[str, Any]:
    record = _new_trade_record(
        symbol=symbol,
        side=side,
        price=price,
        shares=shares,
        record_id=record_id,
        trade_time=trade_time,
        cost_basis=cost_basis,
    )

    def mutate(book: dict[str, Any]) -> None:
        records = list(book.get("trades") or [])
        records.append(record)
        book["trades"] = records

    _book_store().update(_default_state_dict(), mutate)
    return record


def _read_trade_records() -> list[dict[str, Any]]:
    return list(_book_store().load(_default_state_dict()).get("trades") or [])


def _write_trade_records(records: list[dict[str, Any]]) -> None:
    def mutate(book: dict[str, Any]) -> None:
        book["trades"] = list(records)

    _book_store().update(_default_state_dict(), mutate)


def clear_today_trades(today: str | None = None) -> int:
    date_prefix = today or datetime.now().date().isoformat()
    result = {"removed": 0}

    def mutate(book: dict[str, Any]) -> None:
        records = list(book.get("trades") or [])
        kept = [record for record in records if not str(record.get("time") or "").startswith(date_prefix)]
        result["removed"] = len(records) - len(kept)
        book["trades"] = kept

    _book_store().update(_default_state_dict(), mutate)
    return result["removed"]


def revoke_trade(state: QClawState, trade_id: str, persist: bool = True) -> QClawState:
    """Remove one local trade record and reverse its modeled holding effect.

    This only changes the dashboard's local ledger. It never sends a broker order.
    """
    if persist:
        result: dict[str, QClawState] = {}

        def mutate(book: dict[str, Any]) -> None:
            records = list(book.get("trades") or [])
            target = next((record for record in records if str(record.get("id") or "") == str(trade_id)), None)
            if target is None:
                raise ValueError("trade record not found or cannot be revoked")
            current_state = _reverse_trade(_state_from_dict(book["state"]), target)
            book["state"] = asdict(current_state)
            book["trades"] = [record for record in records if record is not target]
            result["state"] = current_state

        _book_store().update(_default_state_dict(), mutate)
        return result["state"]

    records = _read_trade_records()
    target = next((record for record in records if str(record.get("id") or "") == str(trade_id)), None)
    if target is None:
        raise ValueError("trade record not found or cannot be revoked")
    return _reverse_trade(state, target)


def _reverse_trade(state: QClawState, target: dict[str, Any]) -> QClawState:
    symbol = str(target.get("symbol") or "").strip()
    side = _normalise_trade_side(target.get("side"))
    price = _optional_float(target.get("price")) or 0.0
    shares = _optional_float(target.get("shares")) or 0.0
    if not symbol or price <= 0 or shares <= 0:
        raise ValueError("invalid trade record")

    holdings = {str(item.get("symbol")): dict(item) for item in state.holdings if item.get("symbol")}
    current = holdings.get(symbol, {"symbol": symbol, "cost": target.get("cost_basis") or price, "shares": 0})
    current_shares = _optional_float(current.get("shares")) or 0.0
    current_cost = _optional_float(current.get("cost")) or price
    if side == "sell":
        current["shares"] = current_shares + shares
        current["cost"] = _optional_float(target.get("cost_basis")) or current_cost
        holdings[symbol] = current
        state.watchlist = [item for item in state.watchlist if item != symbol]
    elif side == "buy":
        if shares > current_shares:
            raise ValueError("cannot revoke buy: current holding is lower than this trade")
        previous_shares = current_shares - shares
        if previous_shares > 0:
            current["cost"] = ((current_cost * current_shares) - (price * shares)) / previous_shares
            current["shares"] = previous_shares
            holdings[symbol] = current
        else:
            holdings.pop(symbol, None)
            if symbol not in state.watchlist:
                state.watchlist.append(symbol)
    state.holdings = list(holdings.values())
    _ensure_order(state)
    return state


def state_to_items(state: QClawState, auto_lines: dict[str, dict[str, float]] | None = None) -> list[WatchItem]:
    auto_lines = auto_lines or {}
    items: list[WatchItem] = []
    holding_by_symbol = {str(item.get("symbol")): item for item in state.holdings if item.get("symbol")}
    watch_set = set(state.watchlist)
    for symbol in _ordered_symbols(state):
        holding = holding_by_symbol.get(symbol)
        if holding is None and symbol not in watch_set:
            continue
        if not symbol:
            continue
        manual_lines = (state.line_overrides or {}).get(symbol, {})
        lines = auto_lines.get(symbol, {})
        stop = manual_lines.get("stop") or lines.get("stop") or _optional_float((holding or {}).get("stop"))
        target = manual_lines.get("target") or lines.get("target") or _optional_float((holding or {}).get("target"))
        rules: list[AlertRule] = []
        if stop is not None:
            rules.append(AlertRule(symbol=symbol, below=float(stop), label="stop"))
        if target is not None:
            rules.append(AlertRule(symbol=symbol, above=float(target), label="target"))
        if holding is not None:
            items.append(
                WatchItem(
                    symbol=symbol,
                    cost=_optional_float(holding.get("cost")),
                    shares=_optional_float(holding.get("shares")),
                    rules=tuple(rules),
                )
            )
        else:
            items.append(WatchItem(symbol=symbol, rules=tuple(rules)))
    return items


def generate_dashboard(state: QClawState | None = None) -> tuple[Path, dict[str, dict[str, float]]]:
    state = state or read_state()
    ensure_daily_position_snapshot(state)
    auto_lines = build_auto_risk_lines(state)
    output = render_dashboard(
        items=state_to_items(state, auto_lines=auto_lines),
        output_path=REPORT_PATH,
        title="A股持仓与自选决策看板",
        refresh_seconds=5,
    )
    return output, auto_lines


def build_dashboard_snapshot(state: QClawState | None = None) -> dict[str, Any]:
    state = state or read_state()
    trade_date = datetime.now().date().isoformat()
    baseline = ensure_daily_position_snapshot(state, trade_date)
    book = _book_store().load(_default_state_dict())
    today_trades = [
        record
        for record in book.get("trades") or []
        if str(record.get("time") or "").startswith(trade_date)
    ]
    symbols = list(
        dict.fromkeys(
            _ordered_symbols(state)
            + [str(item.get("symbol") or "") for item in baseline]
            + [str(item.get("symbol") or "") for item in today_trades]
        )
    )
    symbols = [symbol for symbol in symbols if symbol]
    quote_error = ""
    try:
        quotes = {quote.symbol: quote for quote in fetch_spot_quotes(symbols)} if symbols else {}
    except requests.RequestException as exc:
        quotes = {}
        quote_error = str(exc)

    auto_lines: dict[str, dict[str, float]] = {}
    holdings = {str(item.get("symbol")): item for item in state.holdings if item.get("symbol")}
    for symbol, quote in quotes.items():
        holding = holdings.get(symbol, {})
        auto_lines[symbol] = auto_risk_line(
            quote,
            cost=_optional_float(holding.get("cost")),
            shares=_optional_float(holding.get("shares")),
        )
    items = state_to_items(state, auto_lines=auto_lines)
    positions = [
        {
            "symbol": item.symbol,
            "cost": item.cost,
            "shares": item.shares,
            "stop": next((rule.below for rule in item.rules if rule.below is not None), None),
            "target": next((rule.above for rule in item.rules if rule.above is not None), None),
            "kind": "holding" if item.shares or item.cost else "watch",
        }
        for item in items
    ]
    quote_payload = {
        symbol: {
            "symbol": quote.symbol,
            "name": quote.name,
            "price": quote.price,
            "prev": quote.prev_close,
            "open": quote.open,
            "change": quote.change,
            "pct": quote.pct_change,
            "high": quote.high,
            "low": quote.low,
            "amount": quote.amount,
            "turnover": quote.turnover,
            "pe": quote.pe,
            "pb": quote.pb,
            "fetchedAt": quote.fetched_at.isoformat(timespec="seconds"),
        }
        for symbol, quote in quotes.items()
    }
    day_snapshot = (book.get("snapshots") or {}).get(trade_date)
    account_snapshot = day_snapshot.get("account") if isinstance(day_snapshot, dict) else None
    metrics = _portfolio_metrics(state, baseline, today_trades, quotes, account_snapshot)
    missing_quotes = [symbol for symbol in symbols if symbol not in quotes]
    return {
        "schemaVersion": 1,
        "revision": book.get("revision", 0),
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
        "tradeDate": trade_date,
        "state": asdict(state),
        "positions": positions,
        "todayTrades": today_trades,
        "dailyBaseline": baseline,
        "accountSnapshot": account_snapshot,
        "quotes": quote_payload,
        "metrics": metrics,
        "autoRiskLines": auto_lines,
        "sourceStatus": {
            "ledger": "ok",
            "quotes": "ok" if not missing_quotes else ("unavailable" if not quotes else "partial"),
            "missingQuotes": missing_quotes,
            "quoteError": quote_error,
        },
    }


def _portfolio_metrics(
    state: QClawState,
    baseline: list[dict[str, Any]],
    today_trades: list[dict[str, Any]],
    quotes: dict[str, SpotQuote],
    account_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    buy_amount = 0.0
    sell_amount = 0.0
    realized_pnl = 0.0
    has_realized_basis = False
    by_symbol: dict[str, dict[str, float]] = {}
    for record in today_trades:
        symbol = str(record.get("symbol") or "")
        side = _normalise_trade_side(record.get("side"))
        price = _optional_float(record.get("price")) or 0.0
        shares = _optional_float(record.get("shares")) or 0.0
        amount = price * shares
        symbol_totals = by_symbol.setdefault(
            symbol,
            {"buyAmount": 0.0, "buyShares": 0.0, "sellAmount": 0.0, "sellShares": 0.0},
        )
        if side == "buy":
            buy_amount += amount
            symbol_totals["buyAmount"] += amount
            symbol_totals["buyShares"] += shares
        elif side == "sell":
            sell_amount += amount
            symbol_totals["sellAmount"] += amount
            symbol_totals["sellShares"] += shares
            cost_basis = _optional_float(record.get("cost_basis"))
            if cost_basis is not None:
                realized_pnl += (price - cost_basis) * shares
                has_realized_basis = True

    t_profit = 0.0
    t_matched_shares = 0.0
    for totals in by_symbol.values():
        matched = min(totals["buyShares"], totals["sellShares"])
        if matched <= 0:
            continue
        average_buy = totals["buyAmount"] / totals["buyShares"]
        average_sell = totals["sellAmount"] / totals["sellShares"]
        t_profit += (average_sell - average_buy) * matched
        t_matched_shares += matched

    market_value = 0.0
    unrealized_pnl = 0.0
    required_current_quotes: set[str] = set()
    for holding in state.holdings:
        symbol = str(holding.get("symbol") or "")
        shares = _optional_float(holding.get("shares")) or 0.0
        cost = _optional_float(holding.get("cost"))
        if shares <= 0:
            continue
        required_current_quotes.add(symbol)
        quote = quotes.get(symbol)
        if quote is None:
            continue
        market_value += quote.price * shares
        if cost is not None:
            unrealized_pnl += (quote.price - cost) * shares

    previous_close_value = 0.0
    required_baseline_quotes: set[str] = set()
    for item in baseline:
        symbol = str(item.get("symbol") or "")
        shares = _optional_float(item.get("shares")) or 0.0
        if shares <= 0:
            continue
        required_baseline_quotes.add(symbol)
        quote = quotes.get(symbol)
        if quote is not None:
            previous_close_value += quote.prev_close * shares

    missing_current_quotes = sorted(
        symbol for symbol in required_current_quotes if symbol not in quotes
    )
    missing_metric_quotes = sorted(
        symbol
        for symbol in required_current_quotes | required_baseline_quotes
        if symbol not in quotes
    )
    position_daily_pnl = None
    if not missing_metric_quotes:
        position_daily_pnl = market_value + sell_amount - buy_amount - previous_close_value

    account_daily_pnl = None
    account_total_assets = None
    account_opening_assets = None
    account_pnl_rate = None
    account_mode = False
    account_as_of = None
    if isinstance(account_snapshot, dict):
        account_opening_assets = _optional_float(account_snapshot.get("openingAssets"))
        account_total_assets = _optional_float(account_snapshot.get("totalAssets"))
        account_daily_pnl = _optional_float(account_snapshot.get("reportedDailyPnl"))
        account_as_of = str(account_snapshot.get("capturedAt") or "")
        account_mode = account_opening_assets is not None and account_daily_pnl is not None
        if account_mode and account_opening_assets:
            account_pnl_rate = account_daily_pnl / account_opening_assets

    daily_pnl = account_daily_pnl if account_mode else position_daily_pnl
    daily_complete = account_mode or not missing_metric_quotes
    return {
        "tradeCount": len(today_trades),
        "buyAmount": round(buy_amount, 3),
        "sellAmount": round(sell_amount, 3),
        "marketValue": round(market_value, 3),
        "previousCloseValue": round(previous_close_value, 3),
        "dailyPnl": round(daily_pnl, 3) if daily_pnl is not None else None,
        "dailyPnlRate": round(account_pnl_rate, 8) if account_pnl_rate is not None else None,
        "dailyPnlSource": "account" if account_mode else "positions",
        "positionDailyPnl": round(position_daily_pnl, 3) if position_daily_pnl is not None else None,
        "accountTotalAssets": round(account_total_assets, 3) if account_total_assets is not None else None,
        "accountOpeningAssets": round(account_opening_assets, 3) if account_opening_assets is not None else None,
        "accountAsOf": account_as_of or None,
        "realizedPnl": round(realized_pnl, 3) if has_realized_basis else None,
        "unrealizedPnl": round(unrealized_pnl, 3) if not missing_metric_quotes else None,
        "tProfit": round(t_profit, 3),
        "tMatchedShares": t_matched_shares,
        "complete": daily_complete,
        "missingQuotes": missing_metric_quotes,
    }


def build_auto_risk_lines(state: QClawState) -> dict[str, dict[str, float]]:
    items = state_to_items(state)
    symbols = [item.symbol for item in items]
    try:
        quotes = {quote.symbol: quote for quote in fetch_spot_quotes(symbols)} if symbols else {}
    except requests.RequestException:
        quotes = {}
    result: dict[str, dict[str, float]] = {}
    holding_by_symbol = {str(item.get("symbol")): item for item in state.holdings}
    for item in items:
        quote = quotes.get(item.symbol)
        if quote is None or quote.price <= 0:
            continue
        holding = holding_by_symbol.get(item.symbol, {})
        cost = _optional_float(holding.get("cost"))
        shares = _optional_float(holding.get("shares"))
        result[item.symbol] = auto_risk_line(quote, cost=cost, shares=shares)
    return result


def auto_risk_line(quote: SpotQuote, cost: float | None = None, shares: float | None = None) -> dict[str, float]:
    price = quote.price
    base = quote.prev_close or price
    day_range = max(quote.high - quote.low, 0.0)
    volatility = max(day_range / base if base else 0.0, abs(quote.pct_change) / 100, 0.025)
    volatility = min(volatility, 0.09)
    if cost is not None and cost > 0 and shares:
        if price >= cost:
            stop = max(cost * 0.97, price * (1 - volatility * 1.15))
            target = max(price * (1 + volatility * 1.45), cost * 1.04)
        else:
            stop = min(cost * 0.94, price * (1 - volatility * 0.9))
            target = max(cost * 0.995, price * (1 + volatility * 1.25))
    else:
        stop = price * (1 - volatility * 1.2)
        target = price * (1 + volatility * 1.6)
    return {"stop": round(stop, 2), "target": round(target, 2)}


def analyze_state(state: QClawState | None = None) -> str:
    state = state or read_state()
    auto_lines = build_auto_risk_lines(state)
    symbols = [item.symbol for item in state_to_items(state, auto_lines=auto_lines)]
    quotes = fetch_spot_quotes(symbols)
    payload = {
        "source": "qclaw-service",
        "holdings": state.holdings,
        "watchlist": state.watchlist,
        "autoRiskLines": auto_lines,
        "quotes": [quote.__dict__ | {"fetched_at": quote.fetched_at.isoformat()} for quote in quotes],
        "dashboard": str(REPORT_PATH),
    }
    analysis = analyze_with_deepseek(payload)
    ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANALYSIS_PATH.write_text(analysis, encoding="utf-8")
    return analysis


def analyze_snapshot(snapshot: dict[str, Any]) -> str:
    analysis = analyze_with_deepseek(snapshot)
    ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ANALYSIS_PATH.write_text(analysis, encoding="utf-8")
    return analysis


def chat_snapshot(snapshot: dict[str, Any], question: str, history: list[dict[str, str]] | None = None) -> str:
    return chat_with_deepseek(snapshot, question, history)


def handle_action(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    state = read_state()
    if action == "import_holdings":
        holdings = list(payload.get("holdings") or [])
        mode = str(payload.get("mode") or "").strip().lower()
        if mode in {"add", "trade", "append"} or _looks_like_trade_import(holdings):
            state = merge_holding_trades(state, holdings)
            dashboard = _try_refresh_dashboard(state)
            return _state_response("交易已按买卖记录累加", state, trade_log=str(TRADE_LOG_PATH), **dashboard)
        state = merge_holdings(state, holdings)
        dashboard = _try_refresh_dashboard(state)
        return _state_response("已按当前持仓覆盖导入", state, **dashboard)
    if action == "replace_holdings_snapshot":
        state = replace_holdings_snapshot(
            state,
            list(payload.get("holdings") or []),
            reset_daily_baseline=bool(payload.get("reset_daily_baseline", True)),
        )
        dashboard = _try_refresh_dashboard(state)
        return _state_response("已按券商快照替换当前持仓", state, **dashboard)
    if action == "import_watchlist":
        state = merge_watchlist(state, [str(item) for item in payload.get("symbols") or []])
        return _state_response("已导入自选股", state)
    if action == "remove_holdings":
        state = remove_holdings(state, [str(item) for item in payload.get("symbols") or []])
        return _state_response("已删除持仓", state)
    if action == "remove_watchlist":
        state = remove_watchlist(state, [str(item) for item in payload.get("symbols") or []])
        return _state_response("已删除自选股", state)
    if action == "clear_watchlist":
        state = clear_watchlist(state)
        return _state_response("已清空自选股", state)
    if action == "update_risk_lines":
        state = update_risk_lines(state, list(payload.get("lines") or []))
        return _state_response("已更新止损/目标覆盖线", state)
    if action == "clear_risk_lines":
        symbols = payload.get("symbols")
        state = clear_risk_lines(state, [str(item) for item in symbols] if symbols else None)
        return _state_response("已恢复自动止损/目标", state)
    if action == "reorder_symbols":
        state = reorder_symbols(state, [str(item) for item in payload.get("symbols") or []])
        return _state_response("已更新股票排序", state)
    if action in {"apply_trade", "buy", "sell"}:
        ensure_daily_position_snapshot(state)
        side = str(payload.get("side") or payload.get("direction") or action)
        state = apply_trade(
            state,
            symbol=str(payload.get("symbol") or ""),
            side=side,
            price=float(payload.get("price") or 0),
            shares=float(payload.get("shares") or 0),
            trade_id=str(payload.get("id") or "") or None,
            trade_time=str(payload.get("time") or "") or None,
        )
        dashboard = _try_refresh_dashboard(state)
        return _state_response("交易已记录", state, trade_log=str(TRADE_LOG_PATH), **dashboard)
    if action == "revoke_trade":
        state = revoke_trade(state, str(payload.get("id") or ""))
        dashboard = _try_refresh_dashboard(state)
        return _state_response("已撤回本地交易记录并还原看板持仓", state, trade_log=str(TRADE_LOG_PATH), **dashboard)
    if action == "clear_today_trades":
        removed = clear_today_trades()
        dashboard = _try_refresh_dashboard(state)
        return _state_response(f"已清空今天 {removed} 笔交易记录（持仓未改动）", state, removed=removed, trade_log=str(TRADE_LOG_PATH), **dashboard)
    if action == "update_account_snapshot":
        account = update_account_snapshot(
            total_assets=float(payload.get("totalAssets", payload.get("total_assets", 0)) or 0),
            market_value=float(payload.get("marketValue", payload.get("market_value", 0)) or 0),
            reported_daily_pnl=float(
                payload.get("reportedDailyPnl", payload.get("reported_daily_pnl", 0)) or 0
            ),
            net_transfer=float(payload.get("netTransfer", payload.get("net_transfer", 0)) or 0),
            trade_date=str(payload.get("tradeDate") or "") or None,
            captured_at=str(payload.get("capturedAt") or "") or None,
        )
        return _state_response(
            "账户总资产基准已保存",
            state,
            account_snapshot=account,
            snapshot=build_dashboard_snapshot(state),
        )
    if action == "generate_dashboard":
        output, auto_lines = generate_dashboard(state)
        return _state_response("看板已生成，止损和目标已自动计算", state, dashboard=str(output), auto_risk_lines=auto_lines)
    if action == "open_dashboard":
        output, auto_lines = generate_dashboard(state)
        return _state_response(
            "看板已准备好，止损和目标已自动计算",
            state,
            dashboard=str(output),
            dashboard_url="http://127.0.0.1:18766/dashboard",
            auto_risk_lines=auto_lines,
        )
    if action == "dashboard_snapshot":
        return _state_response("组合快照已更新", state, snapshot=build_dashboard_snapshot(state))
    if action == "analyze":
        snapshot = payload.get("snapshot")
        analysis = analyze_snapshot(snapshot) if isinstance(snapshot, dict) else analyze_state(state)
        return _state_response("DeepSeek 分析完成", state, analysis=analysis, analysis_path=str(ANALYSIS_PATH))
    if action == "chat":
        snapshot = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else {}
        question = str(payload.get("question") or "")
        history = payload.get("history") if isinstance(payload.get("history"), list) else []
        answer = chat_snapshot(snapshot, question, history)
        return _state_response("AI 对话完成", state, answer=answer, provider="qclaw-deepseek")
    if action == "status":
        return _state_response("当前组合状态", state, dashboard=str(REPORT_PATH))
    return {
        "type": "stock_dashboard_help",
        "message": "可用 action: import_holdings, replace_holdings_snapshot, import_watchlist, remove_holdings, remove_watchlist, clear_watchlist, update_risk_lines, clear_risk_lines, reorder_symbols, apply_trade, buy, sell, revoke_trade, clear_today_trades, update_account_snapshot, generate_dashboard, open_dashboard, dashboard_snapshot, analyze, chat, status",
        "actions": _available_actions(),
    }


class QClawStockHandler(BaseHTTPRequestHandler):
    server_version = "QClawStockDashboard/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in {"/healthz", "/api/health"}:
            book = _book_store().load(_default_state_dict())
            self._send_json({"ok": True, "revision": book.get("revision", 0)})
            return
        if path == "/api/snapshot":
            try:
                self._send_json(build_dashboard_snapshot())
            except Exception as exc:
                self._send_json({"type": "error", "error": str(exc)}, status=500)
            return
        if path in {"/", "/dashboard"}:
            if not REPORT_PATH.exists():
                generate_dashboard()
            self._send_html(REPORT_PATH.read_bytes())
            return
        self._send_json({"error": "not found"}, status=404)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/qclaw/message":
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
            data = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            action = str(data.get("action") or _infer_action(str(data.get("text") or "")))
            payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
            self._send_json(handle_action(action, payload))
        except Exception as exc:
            self._send_json({"type": "error", "error": str(exc)}, status=500)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_qclaw_service(host: str = "127.0.0.1", port: int = 18766) -> None:
    generate_dashboard()
    server = ThreadingHTTPServer((host, port), QClawStockHandler)
    print(f"QClaw stock dashboard service: http://{host}:{port}")
    print("Endpoint: POST /qclaw/message")
    print(f"Dashboard: http://{host}:{port}/dashboard")
    print(f"Snapshot API: http://{host}:{port}/api/snapshot")
    print(f"DeepSeek model: {os.environ.get('DEEPSEEK_MODEL', DEFAULT_DEEPSEEK_MODEL)}")
    print("DeepSeek API key: detected" if load_deepseek_api_key() else "DeepSeek API key: missing")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


def _infer_action(text: str) -> str:
    if "排序" in text or "顺序" in text:
        return "reorder_symbols"
    if ("止损" in text or "止盈" in text or "目标" in text) and ("改" in text or "设置" in text or "调整" in text):
        return "update_risk_lines"
    if ("恢复" in text or "自动" in text) and ("止损" in text or "目标" in text):
        return "clear_risk_lines"
    if "买入" in text or "卖出" in text:
        return "apply_trade"
    if ("删除" in text or "移除" in text) and "持仓" in text:
        return "remove_holdings"
    if ("删除" in text or "移除" in text) and "自选" in text:
        return "remove_watchlist"
    if "清空" in text and "自选" in text:
        return "clear_watchlist"
    if "持仓" in text and "导入" in text:
        return "import_holdings"
    if "自选" in text and "导入" in text:
        return "import_watchlist"
    if "分析" in text:
        return "analyze"
    if "对话" in text or "问" in text:
        return "chat"
    if "打开" in text and "看板" in text:
        return "open_dashboard"
    if "看板" in text or "生成" in text:
        return "generate_dashboard"
    return "status"


def _state_response(message: str, state: QClawState, **extra: Any) -> dict[str, Any]:
    return {
        "type": "stock_dashboard",
        "message": message,
        "state": asdict(state),
        "actions": _available_actions(),
        **extra,
    }


def _try_refresh_dashboard(state: QClawState) -> dict[str, Any]:
    try:
        output, auto_lines = generate_dashboard(state)
        return {"dashboard": str(output), "auto_risk_lines": auto_lines}
    except Exception as exc:
        return {"dashboard": str(REPORT_PATH), "dashboard_warning": str(exc)}


def _available_actions() -> list[dict[str, str]]:
    return [
        {"action": "import_holdings", "label": "导入持仓"},
        {"action": "replace_holdings_snapshot", "label": "替换券商持仓快照"},
        {"action": "import_watchlist", "label": "导入自选"},
        {"action": "remove_holdings", "label": "删除持仓"},
        {"action": "remove_watchlist", "label": "删除自选"},
        {"action": "clear_watchlist", "label": "清空自选"},
        {"action": "update_risk_lines", "label": "修改止损/目标"},
        {"action": "clear_risk_lines", "label": "恢复自动止损/目标"},
        {"action": "reorder_symbols", "label": "调整排序"},
        {"action": "apply_trade", "label": "记录买卖"},
        {"action": "buy", "label": "买入累加"},
        {"action": "sell", "label": "卖出扣减"},
        {"action": "revoke_trade", "label": "撤回本地交易记录"},
        {"action": "clear_today_trades", "label": "清空今日交易记录"},
        {"action": "update_account_snapshot", "label": "更新账户总资产基准"},
        {"action": "generate_dashboard", "label": "生成看板"},
        {"action": "open_dashboard", "label": "打开看板"},
        {"action": "dashboard_snapshot", "label": "刷新组合快照"},
        {"action": "analyze", "label": "触发 DeepSeek 分析"},
        {"action": "chat", "label": "AI 简短对话"},
        {"action": "status", "label": "查看状态"},
    ]


def _optional_float(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    return float(value)


def _valid_symbol(symbol: str) -> bool:
    return len(symbol) == 6 and symbol.isdigit()


def _normalise_trade_side(side: object) -> str:
    text = str(side or "").strip().lower()
    if text in {"buy", "b", "买", "买入", "加仓"}:
        return "buy"
    if text in {"sell", "s", "卖", "卖出", "减仓"}:
        return "sell"
    return text


def _looks_like_trade_import(holdings: list[dict[str, Any]]) -> bool:
    if not holdings:
        return False
    for raw in holdings:
        keys = set(raw)
        side = _normalise_trade_side(raw.get("side") or raw.get("direction"))
        if "price" in keys or side in {"buy", "sell"}:
            return True
    return False


def _known_symbols(state: QClawState) -> set[str]:
    return {str(item.get("symbol")) for item in state.holdings if item.get("symbol")} | set(state.watchlist)


def _ordered_symbols(state: QClawState) -> list[str]:
    known = _known_symbols(state)
    ordered = [symbol for symbol in (state.order or []) if symbol in known]
    for holding in state.holdings:
        symbol = str(holding.get("symbol") or "").strip()
        if symbol and symbol not in ordered:
            ordered.append(symbol)
    for symbol in state.watchlist:
        if symbol not in ordered:
            ordered.append(symbol)
    return ordered


def _ensure_order(state: QClawState) -> None:
    state.order = _ordered_symbols(state)
    state.line_overrides = dict(state.line_overrides or {})


def _remove_from_order_and_overrides(state: QClawState, symbols: set[str]) -> None:
    state.order = [symbol for symbol in (state.order or []) if symbol not in symbols]
    state.line_overrides = {k: v for k, v in dict(state.line_overrides or {}).items() if k not in symbols}
