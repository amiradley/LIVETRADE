from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from typing import Sequence

from .config import Settings, load_settings
from .logging_config import configure_logging
from .mt5_client import ReadOnlyMt5Client
from .storage import MarketDataStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LIVE TRADING read-only MT5 foundation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("check", help="Read terminal and account information")
    subparsers.add_parser("discover", help="Discover and persist broker instruments")

    snapshot = subparsers.add_parser("snapshot", help="Read and persist ticks and OHLC candles")
    snapshot.add_argument("--symbol", action="append", help="Symbol; repeat for multiple symbols")
    snapshot.add_argument("--timeframe", default="M1", choices=ReadOnlyMt5Client.TIMEFRAMES)
    snapshot.add_argument("--bars", type=int, help="Number of candles per symbol")
    return parser


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def run(args: argparse.Namespace, settings: Settings) -> int:
    configure_logging(settings.log_path)
    logger = logging.getLogger(__name__)
    client = ReadOnlyMt5Client(mt5_path=settings.mt5_path)
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
