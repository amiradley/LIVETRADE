# LIVE TRADING — Phase 1

This repository contains the first, read-only foundation of the LIVE TRADING platform. It connects to an installed MetaTrader 5 terminal through the official `MetaTrader5` Python package, reads terminal/account metadata, discovers instruments, retrieves ticks and OHLC candles, and persists snapshots to SQLite.

There is deliberately no order submission, modification, or position-closing path in Phase 1. The default operating mode is research/read-only.

## Prerequisites

- Windows 11
- Python 3.11 or newer
- MetaTrader 5 desktop terminal installed and running
- A broker account may be logged in to the terminal; this phase does not require credentials in environment variables

The current development environment was inspected on 2026-11-17. Python was not available beyond a disabled Windows Store alias, and no MT5 terminal executable or running MT5 process was detected. Therefore, live connectivity could not be verified in this environment.

## Setup

From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set `MT5_PATH` in the environment only if the terminal is not discoverable by the MT5 package. Do not place passwords or API keys in `.env` or source control.

## Read-only commands

```powershell
python -m live_trade check
python -m live_trade discover
python -m live_trade snapshot --symbol EURUSD --timeframe M1 --bars 200
```

Repeat `--symbol` for additional instruments. Snapshot data is stored in `data/market_data.sqlite3`; structured logs are written to `logs/live_trade.jsonl`.

## Tests

```powershell
python -m pytest
```

The tests use a mocked MT5 API and do not connect to a broker or place trades.

## Phase 1 boundaries

- No live trading execution.
- No AI trade decisions.
- No technical-analysis or news claims.
- No hardcoded credentials.
- Market timestamps are normalized to UTC; retrieval time is captured separately as the snapshot timestamp.

Later phases should add analysis and paper trading only after the read-only data path is stable and observable.
