from __future__ import annotations

import argparse
import json
import logging
from typing import Sequence

from .config import Settings, load_settings
from .diagnostics import build_rejection_report
from .dashboard import serve_dashboard
from .finance import FinancialDataService
from .logging_config import configure_logging
from .mt5_client import ReadOnlyMt5Client
from .timestamping import BrokerTimeConfig
from .scanner import MarketScanner, ScannerConfig
from .storage import MarketDataStore
from .storage_manager import StorageManager
from .technical import TechnicalAnalyzer, analyze_multi_timeframe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIVE TRADING read-only MT5 foundation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Read terminal and account information")
    subparsers.add_parser("discover", help="Discover and persist broker instruments")

    snapshot = subparsers.add_parser("snapshot", help="Read and persist ticks and OHLC candles")
    snapshot.add_argument("--symbol", action="append", help="Symbol; repeat for multiple symbols")
    snapshot.add_argument("--timeframe", default="M1", choices=ReadOnlyMt5Client.TIMEFRAMES)
    snapshot.add_argument("--bars", type=int, help="Number of candles per symbol")

    scan = subparsers.add_parser("scan", help="Scan instruments without executing trades")
    scan.add_argument("--symbol", action="append", help="Limit scan; repeat for multiple symbols")
    scan.add_argument("--timeframe", action="append", choices=ReadOnlyMt5Client.TIMEFRAMES)
    scan.add_argument("--bars", type=int, default=100)
    scan.add_argument("--max-active", type=int, default=50)
    scan.add_argument("--max-spread-points", type=float, default=30.0)
    scan.add_argument("--max-tick-age", type=float, default=60.0)
    scan.add_argument(
        "--broker-server-offset-hours",
        type=float,
        default=None,
        help="Override the configured broker server-to-UTC offset for this scan",
    )

    analyze = subparsers.add_parser("analyze", help="Analyse numerical OHLC data without trading")
    analyze.add_argument("--symbol", required=True)
    analyze.add_argument("--timeframe", action="append", choices=ReadOnlyMt5Client.TIMEFRAMES)
    analyze.add_argument("--bars", type=int, default=200)
    analyze.add_argument(
        "--include-live-candle",
        action="store_true",
        help="Include the current forming candle as provisional analysis",
    )
    subparsers.add_parser("financial", help="Read and persist broker financial information")
    dashboard = subparsers.add_parser("dashboard", help="Launch the local graphical dashboard")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--open-browser", action="store_true")
    subparsers.add_parser("storage", help="Run a dry-run project storage and retention scan")
    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def run(args: argparse.Namespace, settings: Settings) -> int:
    configure_logging(settings.log_path)
    if args.command == "dashboard":
        serve_dashboard(settings, host=args.host, port=args.port, open_browser=args.open_browser)
        return 0
    if args.command == "storage":
        _print_json(StorageManager(settings.base_dir).scan().to_dict())
        return 0
    logger = logging.getLogger(__name__)
    client = ReadOnlyMt5Client(mt5_path=settings.mt5_path, broker_time=settings.broker_time)
    try:
        client.connect()
        if args.command == "check":
            _print_json({"terminal": client.terminal_info(), "account": client.account_info()})
        elif args.command == "discover":
            instruments = client.discover_instruments()
            _print_json({"count": len(instruments), "instruments": instruments})
        elif args.command == "snapshot":
            symbols = tuple(item.upper() for item in (args.symbol or settings.symbols))
            if not symbols:
                raise ValueError("Provide --symbol or set LIVE_TRADE_SYMBOLS before snapshot")
            snapshot = client.snapshot(symbols, args.timeframe, args.bars or settings.default_bars)
            run_id = MarketDataStore(settings.db_path).save_snapshot(snapshot)
            _print_json(
                {
                    "run_id": run_id,
                    "captured_at": snapshot.captured_at,
                    "symbols": symbols,
                    "timeframe": args.timeframe,
                    "bars": args.bars or settings.default_bars,
                    "instrument_count": len(snapshot.instruments),
                }
            )
        elif args.command == "scan":
            broker_time = settings.broker_time
            if args.broker_server_offset_hours is not None:
                broker_time = BrokerTimeConfig.with_offset(
                    args.broker_server_offset_hours,
                    source="Explicit CLI scan override",
                )
            config = ScannerConfig(
                timeframes=tuple(args.timeframe or ("M1", "M5")),
                bars=args.bars,
                max_active_instruments=args.max_active,
                max_spread_points=args.max_spread_points,
                max_tick_age_seconds=args.max_tick_age,
                broker_time=broker_time,
            )
            scan_run = MarketScanner(client, config).scan(tuple(args.symbol) if args.symbol else None)
            scan_id = MarketDataStore(settings.db_path).save_scan(scan_run)
            report = build_rejection_report(scan_run)
            _print_json(
                {
                    "scan_id": scan_id,
                    "discovered_count": scan_run.discovered_count,
                    "result_count": len(scan_run.results),
                    "eligible_count": sum(result.eligible for result in scan_run.results),
                    "activated_count": sum(result.activated for result in scan_run.results),
                    "timeframes": config.timeframes,
                    "broker_server_offset_hours": broker_time.utc_offset_hours,
                    "broker_time": broker_time.to_dict(),
                    "rejection_report": report.to_dict(),
                }
            )
        elif args.command == "analyze":
            timeframes = tuple(args.timeframe or ReadOnlyMt5Client.TIMEFRAMES)
            candles_by_timeframe = {}
            unavailable = {}
            for timeframe in timeframes:
                try:
                    candles_by_timeframe[timeframe] = client.get_candles(args.symbol.upper(), timeframe, args.bars)
                except Exception as exc:
                    unavailable[timeframe] = str(exc)
            analyzer = TechnicalAnalyzer()
            analyses = {
                timeframe: analyzer.analyze(
                    args.symbol.upper(), timeframe, candles, closed_only=not args.include_live_candle
                )
                for timeframe, candles in candles_by_timeframe.items()
            }
            multi_timeframe = analyze_multi_timeframe(
                args.symbol.upper(),
                candles_by_timeframe,
                analyzer,
                closed_only=not args.include_live_candle,
            )
            store = MarketDataStore(settings.db_path)
            analysis_ids = [store.save_technical_analysis(analysis) for analysis in analyses.values()]
            multi_timeframe_id = store.save_multi_timeframe_analysis(multi_timeframe)
            _print_json(
                {
                    "symbol": args.symbol.upper(),
                    "analysis_ids": analysis_ids,
                    "multi_timeframe_id": multi_timeframe_id,
                    "unavailable_timeframes": unavailable,
                    "analysis": multi_timeframe.to_dict(),
                }
            )
        elif args.command == "financial":
            snapshot = FinancialDataService(client).capture()
            snapshot_id = MarketDataStore(settings.db_path).save_financial_snapshot(snapshot)
            _print_json(
                {
                    "snapshot_id": snapshot_id,
                    "captured_at": snapshot.captured_at,
                    "account_type": snapshot.account_type,
                    "currency": snapshot.account_currency,
                    "balance": snapshot.balance,
                    "equity": snapshot.equity,
                    "margin_used": snapshot.margin_used,
                    "free_margin": snapshot.free_margin,
                    "floating_pnl": snapshot.floating_pnl,
                    "open_positions": len(snapshot.positions),
                    "data_quality": snapshot.data_quality,
                }
            )
        return 0
    except Exception:
        logger.exception("Read-only MT5 command failed")
        raise
    finally:
        client.disconnect()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = load_settings()
    try:
        return run(args, settings)
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
