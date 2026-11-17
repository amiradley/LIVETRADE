# LIVE TRADING — Phase 1

This repository contains the first, read-only foundation of the LIVE TRADING platform. It connects to an installed MetaTrader 5 terminal through the official `MetaTrader5` Python package, reads terminal/account metadata, discovers instruments, retrieves ticks and OHLC candles, and persists snapshots to SQLite.

There is deliberately no order submission, modification, or position-closing path in Phase 1. The default operating mode is research/read-only.

## Prerequisites

- Windows 11
- Python 3.11 or newer
- MetaTrader 5 desktop terminal installed and running
- A broker account may be logged in to the terminal; this phase does not require credentials in environment variables

MT5 IPC requires Python and the terminal to run under the same Windows user and session with compatible integrity levels. A low-integrity project shell may time out even when MT5 is visibly open. Do not run the application as Administrator merely to bypass this; use a normal Windows PowerShell session instead. See [docs/architecture.md](docs/architecture.md) for the launch procedure and future-control architecture.

## Setup

From the project root in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

For live MT5 verification, open a normal Windows PowerShell window under the same user that owns the MT5 terminal, then run:

```powershell
Set-Location 'C:\Users\USER\Desktop\LIVE TRADE'
& '.\.venv\Scripts\python.exe' -m live_trade check
& '.\.venv\Scripts\python.exe' -m live_trade snapshot --symbol EURUSD --timeframe M1 --bars 10
```

The terminal must already be running in the same Windows session. Avoid an Administrator launch unless the terminal itself is also intentionally run at that level.

Set `MT5_PATH` in the environment only if the terminal is not discoverable by the MT5 package. Do not place passwords or API keys in `.env` or source control.

## Read-only commands

```powershell
python -m live_trade check
python -m live_trade discover
python -m live_trade snapshot --symbol EURUSD --timeframe M1 --bars 200
python -m live_trade scan --symbol EURUSD --timeframe M1 --timeframe M5 --bars 100
```

Repeat `--symbol` for additional instruments. Snapshot data is stored in `data/market_data.sqlite3`; structured logs are written to `logs/live_trade.jsonl`.

The Phase 2 scanner evaluates all discovered instruments when no `--symbol` filter is provided, but only progressively activates the best candidates for detailed candle retrieval. It currently applies deterministic visibility, trade-mode, weekend-session, tick-freshness, point-size, and spread gates. It does not calculate indicators or execute trades yet.

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
