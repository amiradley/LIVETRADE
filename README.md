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
python -m live_trade scan --symbol EURUSD --timeframe M1 --bars 10 --broker-server-offset-hours 2
python -m live_trade analyze --symbol EURUSD --timeframe M1 --timeframe M5 --bars 200
python -m live_trade financial
python -m live_trade dashboard --open-browser
```

Repeat `--symbol` for additional instruments. Snapshot data is stored in `data/market_data.sqlite3`; structured logs are written to `logs/live_trade.jsonl`.

The Phase 2 scanner evaluates all discovered instruments when no `--symbol` filter is provided, but only progressively activates the best candidates for detailed candle retrieval. It currently applies deterministic visibility, trade-mode, weekend-session, tick-freshness, point-size, and spread gates. It does not calculate indicators or execute trades yet.

`--broker-server-offset-hours` is an explicit timestamp interpretation setting, not a future-tick tolerance. The default is zero and future timestamps remain rejected. Use a non-zero value only after comparing raw MT5 tick and candle epochs with the Windows/Python clocks for the active broker terminal.

The `analyze` command runs the numerical technical-analysis foundation over OHLC candles and persists indicator, pattern, structure, score, and multi-timeframe records. Confirmed analysis excludes the current forming candle by default; use `--include-live-candle` only for explicitly provisional observations. It remains read-only and does not produce execution decisions.

The `financial` command reads broker account and open-position information from MT5 and stores a local financial snapshot. It does not calculate or display invented currency conversions, and it does not place or close trades.

## Graphical dashboard

The Stage 1.5 dashboard is a local browser-based desktop interface. It uses the existing Python backend and SQLite store, binds only to `127.0.0.1`, and has no order-mutation API. It shows broker-verified account values, explicit data-quality labels, read-only positions, persisted scanner/analysis records, and the safe research/demo-shadow session controls.

From a normal Windows PowerShell window running under the same user and session as MT5:

```powershell
Set-Location 'C:\Users\USER\Desktop\LIVE TRADE'
& '.\.venv\Scripts\python.exe' -m live_trade dashboard --open-browser
```

For a desktop-style launch, double-click `run_dashboard.cmd`. The launcher refuses a Low-integrity context, warns if `terminal64.exe` is not running, and does not request Administrator privileges. A second dashboard instance is rejected if the default port is already in use.

The dashboard's START button begins only the verified research/demo-shadow workflow. STOP ends that workflow without placing, modifying, or closing broker positions. Currency conversion to MYR, realised-deal history, profit allocation, and actual execution remain explicitly pending or unavailable until their later milestones.

Stage 1.6 adds read-only broker deal-history reconciliation, partial-close aggregation, a persistent 20% savings-reserve high-water-mark ledger, and USD/MYR conversion through the configured exchange-rate source. Broker currency values remain the source of truth; converted MYR values are labelled estimates and become unavailable if the rate is stale or cannot be retrieved. No withdrawal, transfer, or order API exists.

Stage 1.6A adds end-to-end demo-shadow health reporting. START now starts the read-only scanner, closed-candle technical-analysis, and market-monitoring worker; STOP joins it before the final snapshot. Component cycle counts and thread liveness are exposed through `/api/status`, and MT5 failures transition the session to an explicit error/stale state.

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
