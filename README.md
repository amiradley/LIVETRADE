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
python -m live_trade scan --symbol EURUSD --timeframe M1 --bars 10
python -m live_trade analyze --symbol EURUSD --timeframe M1 --timeframe M5 --bars 200
python -m live_trade financial
python -m live_trade instrument-audit
python -m live_trade history-audit --symbol EURUSD --timeframe M1 --timeframe M5 --bars 200
python -m live_trade collection-plan --symbol EURUSD --timeframe M1 --timeframe M5 --bars 500
python -m live_trade collect --symbol EURUSD --timeframe M1 --timeframe M5 --bars 500 --max-batches 1
python -m live_trade collection-status
python -m live_trade research-run --symbol EURUSD --timeframe M1 --limit 4
python -m live_trade research-status
python -m live_trade dashboard --open-browser
```

Repeat `--symbol` for additional instruments. Snapshot data is stored in `data/market_data.sqlite3`; structured logs are written to `logs/live_trade.jsonl`.

The Phase 2 scanner evaluates all discovered instruments when no `--symbol` filter is provided, but only progressively activates the best candidates for detailed candle retrieval. It currently applies deterministic visibility, trade-mode, weekend-session, tick-freshness, point-size, and spread gates. It does not calculate indicators or execute trades yet.

The default MetaQuotes-Demo configuration uses the verified UTC+2 broker-server offset recorded in the timestamp configuration. `--broker-server-offset-hours` remains available as an explicit, auditable per-scan override; it is not a future-tick tolerance. Unknown broker servers require an explicit `LIVE_TRADE_BROKER_UTC_OFFSET_HOURS` setting, and raw timestamps remain preserved alongside effective UTC timestamps. At presentation boundaries, raw MT5 broker labels are converted once to effective UTC and then displayed in `Asia/Kuala_Lumpur` (UTC+8); SQLite records are never rewritten for display.

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

Stage 1.7 adds instrument intelligence, historical coverage audits, quality validation, a resumable historical-candle store, collection priorities, persisted collection jobs, and dashboard data-intelligence controls. The default collection commands are bounded and read-only. The scheduler is persistence-backed but intentionally not an operating-system service: collection runs only while explicitly started from the CLI or dashboard.

Stage 1.8 adds deterministic instrument-priority tiers, configurable historical targets, bounded paged coverage audits, and explicit research-readiness state without changing the read-only boundary. `INITIAL`, `INTERMEDIATE`, and `EXTENDED` targets are defined per supported timeframe. Coverage records retain local and broker counts, target level, source status, dataset version, and provenance, so broker availability cannot be mistaken for local research readiness. The collector pauses at the storage protection threshold, resumes from persisted positions, and never deletes or silently repairs flagged data. M1 expansion is intentionally deferred until the higher-timeframe pilot and quality findings are reviewed.

Stage 1.8A replaces moving positional pagination after the newest page with time-anchored MT5 range requests. Each page records its request parameters, raw/effective boundaries, accepted count, forming-candle count, overlap/duplicate/order/gap findings, retry count, duration, MT5 error details, and result. Boundary overlaps and unexpected ordering reject the page and preserve the diagnostic evidence; they are not silently deduplicated. Transient MT5 failures receive a bounded retry budget, while forming candles are excluded from research persistence by default and remain visible in batch diagnostics.

Stage 1.8B adds bounded collection sessions for progressive Tier 1 expansion. A session records its scope, limits, start/end state, request and quality counters, storage growth, and stop reason. CLI collection sessions default to H1/M15, a finite batch budget, a candle cap, a duration cap, and a storage-growth cap; the session processes only the jobs in its explicit plan and does not drain unrelated queued work. Session history is available through `collection-status` and `collection-sessions`, while the dashboard shows recent session outcomes and batch diagnostics. These controls support repeated resumable sessions without implying that increased candle count is research readiness.

Stage 1.8C adds Windows-native, read-only resource snapshots to bounded sessions. System RAM and disk usage are sampled with explicit warning/protection/critical states; SQLite growth remains independently capped. Process RAM and CPU are reported as unavailable when a safe native measurement is not available, never as zero. Protection states pause new collection requests without deleting data. The four pilot instruments and H1/M15 scope remain the only expansion scope.

The first-demo pathway is represented by the read-only trial-readiness report and the trial-readiness API endpoint. It distinguishes data, research, strategy, validation, risk-control, execution, STOP/recovery, monitoring, and authorization gates. The report cannot approve a strategy or enable execution; the current MT5 adapter remains read-only.

The guarded DEMO execution pathway is separate from DEMO_SHADOW. It uses DemoOnlyExecutionAdapter, which enforces demo identity, terminal permissions, broker lot/tick specifications, spread and protective-stop checks, the RM100-equivalent budget, duplicate signal protection, persistent order state, and broker-position reconciliation. Dashboard START remains shadow-only; the separate Demo Execution panel cannot submit while terminal automated trading is disabled.

The research intelligence layer provides reproducible hypothesis records, bounded OHLC backtests with explicit spread/slippage assumptions, train/out-of-sample separation, validation outcomes, evidence records, and persistent learning observations. It is deliberately conservative: small samples produce inconclusive results, rejected hypotheses remain stored, and no research status grants execution approval.

The live scanner records a symbol's capture time at that symbol's tick-retrieval boundary. It does not compare later symbol ticks with the start of a multi-symbol scan, which could otherwise create a false future-tick rejection. The strict future-timestamp guard remains fail-closed. The experimental per-trade budget is preserved as an existing setting; its legacy 4.0935 conversion differs from the persisted 4.1195 risk-limit reference and must be reconciled only through an explicitly approved configuration change.

Decision records expose explicit qualification categories: `QUALIFIED`, `REJECTED — STRATEGY CONDITION`, `DEFERRED — INSUFFICIENT DATA`, and `DATA INVALID`. Account-risk and execution-safety blocks remain separate operational gates and are not relabeled as strategy rejections.

### DEMO risk limits and monitoring

The approved DEMO loss limits are persisted in SQLite and remain separate from manual entry activation:

- Daily realised loss: RM4.88.
- Weekly realised loss: RM9.77.
- Maximum cumulative drawdown: RM19.54.
- Reset timezone: Malaysia time, `Asia/Kuala_Lumpur` (UTC+8, no daylight-saving changes).

At configuration time these RM limits are converted to USD using the recorded USD/MYR exchange-rate row. The current verified reference is `1 USD = 4.1195 MYR`, giving USD limits of approximately `$1.18461`, `$2.37165`, and `$4.74329`. Daily and weekly checks use net closing-deal P&L. Drawdown uses persisted peak account equity minus current equity, so realised and floating P&L are not double-counted. Missing, stale, or invalid rate/account data blocks new entries. Existing positions remain under their broker-confirmed protective SL/TP policy.

The monitoring worker is single-instance, restartable, and exposes cycle/error state through `/api/monitoring`. New DEMO entries remain disabled until a separate explicit activation and all safety gates pass; monitoring and analysis do not submit orders.

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
- Market timestamps preserve the raw MT5 epoch and use the central broker-time configuration for effective UTC validation. Retrieval time is captured separately as the snapshot timestamp.
- `python -m live_trade storage` performs a dry-run-only retention scan. Protected SQLite/source/configuration files are never cleanup candidates; temporary files are reviewed after 7 days, diagnostic logs after 30 days, and research archives after 90 days. The dashboard exposes the same report without destructive actions.
- Historical quality reports distinguish incomplete forming candles from invalid data and ignore normal weekend closures as expected market-session gaps. A `READY` dataset means structurally usable historical data only; it is not evidence of profitability or a trading edge.

Later phases should add analysis and paper trading only after the read-only data path is stable and observable.
