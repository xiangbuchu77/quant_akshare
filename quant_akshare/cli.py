from __future__ import annotations

import argparse
from datetime import datetime
import time
from pathlib import Path

import requests

from .ai_server import run_ai_server
from .backtest import run_ma_backtest
from .data_source import fetch_stock_daily
from .dashboard import render_dashboard
from .models import BacktestConfig
from .qclaw_service import run_qclaw_service
from .realtime import (
    build_watch_items,
    fetch_spot_quotes,
    format_watch_row,
    parse_symbol_map,
)
from .storage import read_prices, write_prices
from .strategy import latest_signal, moving_average_signal


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "market.db"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "reports"


def load_or_fetch(config: BacktestConfig, db_path: Path, adjust: str) -> object:
    prices = read_prices(db_path, config.symbol, config.start, config.end)
    if prices.empty:
        prices = fetch_stock_daily(config.symbol, config.start, config.end, adjust=adjust)
        write_prices(db_path, config.symbol, prices)
    return prices


def cmd_fetch(args: argparse.Namespace) -> None:
    prices = fetch_stock_daily(args.symbol, args.start, args.end, adjust=args.adjust)
    write_prices(args.db, args.symbol, prices)
    print(f"Fetched {len(prices)} rows for {args.symbol} into {args.db}")


def cmd_backtest(args: argparse.Namespace) -> None:
    config = BacktestConfig(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        short_window=args.short_window,
        long_window=args.long_window,
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_rate=args.slippage_rate,
    )
    prices = load_or_fetch(config, args.db, args.adjust)
    daily, trades, perf = run_ma_backtest(
        prices,
        short_window=config.short_window,
        long_window=config.long_window,
        initial_cash=config.initial_cash,
        fee_rate=config.fee_rate,
        slippage_rate=config.slippage_rate,
    )

    args.report_dir.mkdir(parents=True, exist_ok=True)
    daily_path = args.report_dir / f"{args.symbol}_backtest.csv"
    trades_path = args.report_dir / f"{args.symbol}_trades.csv"
    daily.to_csv(daily_path, index=False)
    trades.to_csv(trades_path, index=False)

    print(f"Symbol: {args.symbol}")
    print(f"Rows: {len(daily)}")
    print(f"Final equity: {perf.final_equity:.2f}")
    print(f"Total return: {perf.total_return:.2%}")
    print(f"Annual return: {perf.annual_return:.2%}")
    print(f"Max drawdown: {perf.max_drawdown:.2%}")
    print(f"Trades: {perf.trade_count}")
    print(f"Daily report: {daily_path}")
    print(f"Trades report: {trades_path}")


def cmd_signal(args: argparse.Namespace) -> None:
    config = BacktestConfig(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        short_window=args.short_window,
        long_window=args.long_window,
    )
    prices = load_or_fetch(config, args.db, args.adjust)
    signals = moving_average_signal(prices, args.short_window, args.long_window)
    row = signals.iloc[-1]
    print(f"Symbol: {args.symbol}")
    print(f"Date: {row['date'].date()}")
    print(f"Close: {row['close']:.2f}")
    print(f"MA{args.short_window}: {row['ma_short']:.2f}")
    print(f"MA{args.long_window}: {row['ma_long']:.2f}")
    print(f"Signal: {latest_signal(signals)}")


def cmd_watch(args: argparse.Namespace) -> None:
    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
    items = build_watch_items(
        symbols=symbols,
        costs=parse_symbol_map(args.costs),
        shares=parse_symbol_map(args.shares),
        stop_lines=parse_symbol_map(args.stops),
        target_lines=parse_symbol_map(args.targets),
    )
    item_by_symbol = {item.symbol: item for item in items}
    loops = 1 if args.once else args.loops
    loop_index = 0
    error_count = 0

    while True:
        try:
            quotes = fetch_spot_quotes(
                symbols,
                timeout=args.timeout,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
            error_count = 0
            for quote in quotes:
                item = item_by_symbol.get(quote.symbol)
                if item is None:
                    continue
                print(format_watch_row(quote, item), flush=True)
            loop_index += 1
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            error_count += 1
            print(
                f"{datetime.now():%H:%M:%S} | 行情请求失败 "
                f"({error_count}/{args.max_errors}): {exc}",
                flush=True,
            )
            if args.once or error_count >= args.max_errors:
                raise SystemExit(2) from exc
        if args.once or (loops is not None and loop_index >= loops):
            break
        time.sleep(args.interval)


def cmd_dashboard(args: argparse.Namespace) -> None:
    symbols = [symbol.strip() for symbol in args.symbols.split(",") if symbol.strip()]
    watchlist = [symbol.strip() for symbol in args.watchlist.split(",") if symbol.strip()]
    for symbol in watchlist:
        if symbol not in symbols:
            symbols.append(symbol)
    items = build_watch_items(
        symbols=symbols,
        costs=parse_symbol_map(args.costs),
        shares=parse_symbol_map(args.shares),
        stop_lines=parse_symbol_map(args.stops),
        target_lines=parse_symbol_map(args.targets),
    )
    output = render_dashboard(
        items=items,
        output_path=args.output,
        title=args.title,
        refresh_seconds=args.interval,
    )
    print(f"Dashboard: {output}")


def cmd_ai_server(args: argparse.Namespace) -> None:
    run_ai_server(host=args.host, port=args.port)


def cmd_qclaw_service(args: argparse.Namespace) -> None:
    run_qclaw_service(host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AKShare quant starter")
    subparsers = parser.add_subparsers(required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--symbol", required=True, help="A-share code, e.g. 000001")
        subparser.add_argument("--start", required=True, help="YYYYMMDD")
        subparser.add_argument("--end", required=True, help="YYYYMMDD")
        subparser.add_argument("--adjust", default="qfq", choices=["", "qfq", "hfq"])
        subparser.add_argument("--db", type=Path, default=DEFAULT_DB)

    fetch = subparsers.add_parser("fetch", help="Fetch data from AKShare")
    add_common(fetch)
    fetch.set_defaults(func=cmd_fetch)

    backtest = subparsers.add_parser("backtest", help="Run MA crossover backtest")
    add_common(backtest)
    backtest.add_argument("--short-window", type=int, default=20)
    backtest.add_argument("--long-window", type=int, default=60)
    backtest.add_argument("--initial-cash", type=float, default=100_000.0)
    backtest.add_argument("--fee-rate", type=float, default=0.0003)
    backtest.add_argument("--slippage-rate", type=float, default=0.0005)
    backtest.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    backtest.set_defaults(func=cmd_backtest)

    signal = subparsers.add_parser("signal", help="Print latest signal")
    add_common(signal)
    signal.add_argument("--short-window", type=int, default=20)
    signal.add_argument("--long-window", type=int, default=60)
    signal.set_defaults(func=cmd_signal)

    watch = subparsers.add_parser("watch", help="Watch real-time quotes and alerts")
    watch.add_argument(
        "--symbols",
        required=True,
        help="Comma separated A-share codes, e.g. 002463,600030",
    )
    watch.add_argument(
        "--costs",
        default="",
        help="Optional cost map, e.g. 002463=129.156,600030=26.0",
    )
    watch.add_argument(
        "--shares",
        default="",
        help="Optional share map, e.g. 002463=400,600030=300",
    )
    watch.add_argument(
        "--stops",
        default="",
        help="Optional stop lines, e.g. 002463=124,600030=24.8",
    )
    watch.add_argument(
        "--targets",
        default="",
        help="Optional target lines, e.g. 002463=132,600030=27.4",
    )
    watch.add_argument("--interval", type=float, default=5.0, help="Refresh seconds")
    watch.add_argument("--loops", type=int, default=None, help="Stop after N loops")
    watch.add_argument("--once", action="store_true", help="Fetch one snapshot")
    watch.add_argument("--timeout", type=float, default=8.0, help="Request timeout seconds")
    watch.add_argument("--retries", type=int, default=2, help="Retries per refresh")
    watch.add_argument("--retry-delay", type=float, default=0.5, help="Initial retry delay seconds")
    watch.add_argument("--max-errors", type=int, default=10, help="Stop after N consecutive refresh failures")
    watch.set_defaults(func=cmd_watch)

    dashboard = subparsers.add_parser("dashboard", help="Generate an HTML portfolio dashboard")
    dashboard.add_argument("--symbols", required=True, help="Comma separated A-share codes")
    dashboard.add_argument("--watchlist", default="", help="Optional no-position symbols, e.g. 300750,000725")
    dashboard.add_argument("--costs", default="", help="Cost map, e.g. 002463=147.635")
    dashboard.add_argument("--shares", default="", help="Share map, e.g. 002463=200")
    dashboard.add_argument("--stops", default="", help="Stop lines, e.g. 002463=132")
    dashboard.add_argument("--targets", default="", help="Target lines, e.g. 002463=140")
    dashboard.add_argument("--interval", type=int, default=5, help="Browser refresh seconds")
    dashboard.add_argument("--title", default="A股持仓决策看板")
    dashboard.add_argument("--output", type=Path, default=DEFAULT_REPORT_DIR / "portfolio_dashboard.html")
    dashboard.set_defaults(func=cmd_dashboard)

    ai_server = subparsers.add_parser("ai-server", help="Run local DeepSeek analysis service")
    ai_server.add_argument("--host", default="127.0.0.1")
    ai_server.add_argument("--port", type=int, default=18765)
    ai_server.set_defaults(func=cmd_ai_server)

    qclaw_service = subparsers.add_parser("qclaw-service", help="Run QClaw/OpenClaw API-only stock dashboard service")
    qclaw_service.add_argument("--host", default="127.0.0.1")
    qclaw_service.add_argument("--port", type=int, default=18766)
    qclaw_service.set_defaults(func=cmd_qclaw_service)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
