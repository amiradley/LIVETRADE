# LIVE TRADING Architecture Notes

## Normal MT5 launch context

The Python process and MT5 terminal must use the same Windows user and session. They also need compatible Windows integrity levels for MT5 IPC. In testing, the terminal ran at Medium integrity while the Codex project shell launched Python at Low integrity; `initialize()` timed out in the Low context and succeeded in a normal Medium context.

Recommended local procedure:

1. Start MT5 and log in to the demo account.
2. Open a normal Windows PowerShell session as the same user. Do not use an Administrator window as a workaround.
3. Change to the project directory.
4. Run the project virtual-environment Python executable directly.
5. Confirm `live_trade check` before any future session start.

The current Phase 1 code remains read-only. If the current agent environment cannot provide a compatible process context, the owner should run the verification command from normal Windows PowerShell.

## Trading Session Controller — planned boundary

The controller is a future orchestration layer, separate from MT5 market-data access, analysis, risk controls, and execution. Its state machine is:

`STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`

Failures transition to `ERROR`; an unresolved broker-side position during shutdown transitions to `UNRESOLVED_POSITIONS`. Duplicate starts and concurrent sessions are rejected.

The future START flow must pass connectivity, account, market-data, risk, and required news-health checks before enabling any permitted execution mode. STOP must first prohibit new entries, inspect positions, attempt policy-driven closure, verify broker state, record realised P&L, generate the daily report, persist records, stop services, and disconnect. The emergency switch is a separate execution-suspension control and does not silently liquidate positions.

No session-controller or execution code is enabled in Phase 1.1.

## Stage 1 financial and session backend

`FinancialDataService` exposes broker-reported account currency, balance, equity, margin, free margin, open positions, and floating P&L as a read-only dashboard model. Original capital, realised P&L, retained profit, savings reserve, and MYR conversion are intentionally separate future ledgers; no missing value is silently estimated.

`TradingSessionController` currently supports only `RESEARCH` and `DEMO_SHADOW` modes. START performs connection, account, market-data, historical-data, and database checks before recording a running session. STOP inspects broker positions; with no positions it safely disconnects, while any remaining position produces `UNRESOLVED_POSITIONS` because this stage has no close-order capability. The controller exposes no order mutation methods.

The future dashboard will use this backend as a read model with exactly two primary controls, START and STOP. Emergency suspension, liquidation, deal-history reconciliation, and execution remain later explicitly authorized stages.

## Stage 1.5 local dashboard

The graphical dashboard is served by `live_trade dashboard` from a local Python `ThreadingHTTPServer` bound to `127.0.0.1`. Static HTML, CSS, and JavaScript provide the interface; no frontend build toolchain or extra runtime is required. The API delegates financial capture and session transitions to the existing read-only client, `FinancialDataService`, `TradingSessionController`, and `MarketDataStore`.

The dashboard refreshes broker financial values through a short-lived MT5 read connection when stopped, and keeps the session controller's read-only connection while a demo-shadow session is running. Disconnections are surfaced as `DISCONNECTED` or `STALE`; the UI never presents a stale snapshot as live. Broker values, internal calculations, pending ledgers, and unavailable conversions are labelled separately. The local API exposes only GET read models plus the safe research/demo-shadow START and STOP transitions; it contains no order, deal, withdrawal, or transfer endpoint.

`run_dashboard.cmd` calls `run_dashboard.ps1`. The launcher checks for a Low-integrity context and stops with an actionable message because MT5 IPC previously required a Medium-integrity Python process under the same user and Windows session. It does not elevate the process or change MT5 settings.

## Stage 1.6 financial intelligence

`ReadOnlyMt5Client.history_deals` reads the broker's deal history through `history_deals_get`; it does not expose order submission or mutation. `accounting.py` groups entry and exit deals by broker position identifier, aggregates partial closes, and preserves gross profit, commission, swap, fee, timestamps, prices, volume, and deal identifiers. Raw broker deals are deduplicated in SQLite by deal ticket.

`intelligence.py` obtains USD/MYR through the configured rate endpoint, records source and quote/retrieval dates, validates positive finite values, and reports `UNAVAILABLE` or `STALE` rather than falling back to an invented rate. The savings ledger is internal only: eligible cumulative realised net profit above the persisted high-water mark is split 20% reserve / 80% retained profit. Losses do not create allocations, and recovery must exceed the previous high-water mark before a new allocation is recorded.

## Stage 1.6A health and runtime integration

The demo-shadow START transition now owns a daemon research worker that runs the read-only scanner, closed-candle technical analysis, and market-monitoring cycle under a client lock. Component health exposes cycle count, thread liveness, last completion, and last error. STOP signals and joins the worker before the final financial snapshot; a failed active-session read transitions to `ERROR` and marks the persisted session record accordingly rather than claiming `RUNNING`. Broker time is centralized in `BrokerTimeConfig`; the verified MetaQuotes-Demo UTC+2 configuration is passed to the scanner and worker, while unknown brokers remain unconfigured and reject timestamp validation until explicitly configured.

## Phase 2 scanner foundation

`MarketScanner` is a read-only policy layer over the MT5 adapter. It discovers instruments, reads ticks, calculates spread in broker points, rejects stale or invalid data, labels a coarse UTC market session, progressively activates only the lowest-spread eligible candidates, retrieves configured OHLC timeframes, and persists a scan run plus per-symbol results. The scanner does not place orders and does not claim that eligibility implies profitability.

The first scanner increment supports M1, M5, M15, M30, H1, H4, and D1 retrieval and stores candle counts/freshness. Technical indicators, chart-pattern evidence, and multi-timeframe ranking will be added as separate deterministic stages after this data-quality foundation.

Future-dated tick timestamps are intentionally rejected. MT5/broker server clock offsets must be measured and supplied as explicit, audited configuration before any offset-aware activation is allowed; the scanner must not silently reinterpret timestamps.

The current MetaQuotes-Demo observation measured a stable two-hour offset: raw tick and M1 candle epochs both converted to UTC were two hours ahead of the Python UTC clock, while Windows local time was UTC+08:00. This is recorded as an auditable project default; the CLI override remains available for a separately verified terminal context. Scan records persist the offset, configuration state, and source used for interpretation.

The Storage Manager is a dry-run-only project health and retention report. It reports disk thresholds at 70%, 85%, and 90%, protects the database and application files, and classifies temporary, diagnostic, archive, and unclassified files for review. `python -m live_trade storage` and the dashboard Storage Manager page never delete files; every scan is appended to the storage audit log.

## Phase 2.2 technical-analysis foundation

`TechnicalAnalyzer` operates only on validated numerical OHLC candles. It calculates SMA 20/50, EMA 9/20/50, RSI 14, MACD 12/26/9, ATR 14, ATR percentage, candle range, and recent volatility change. It detects only rule-based engulfing, hammer, shooting-star, doji, and strong-candle formations with numerical evidence. It also reports measurable higher/lower structure, support/resistance, breakouts, false breakouts, multi-timeframe agreement, and a provisional technical score with factor contributions.

The score is a research measurement, not a probability of profit and not an execution permission. Insufficient history, invalid OHLC values, NaN, and infinite values remain explicit data-quality failures. Technical records are deduplicated by symbol, timeframe, and source-candle timestamp.

Confirmed analysis excludes the current forming MT5 candle by default and records `candle_scope=CLOSED`. Including the current candle requires the explicit `--include-live-candle` option and is labelled `INCLUDING_LIVE`; it is provisional and must not be treated as confirmed pattern evidence.

## Profit Allocation Manager — planned accounting boundary

The future manager is an accounting ledger only. It allocates 20% of eligible cumulative realised net profit to a savings reserve and retains 80% as trading capital. It excludes unrealised P&L, deposits, borrowed funds, and duplicate broker deals. Losses reduce cumulative profitability and must be recovered before new allocations become eligible.

Each allocation will have a unique transaction ID, broker deal reference, timestamp, realised P&L, costs, eligible cumulative profit, savings amount, retained amount, and status. A cumulative high-water mark prevents duplicate allocations.

The dashboard must show trading balance, retained trading capital, reserved savings, and actual withdrawn savings separately. No automatic withdrawal or bank transfer is planned or implemented.
