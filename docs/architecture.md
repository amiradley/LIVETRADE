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

For live multi-symbol scans, each tick is evaluated against the UTC capture instant immediately after that symbol's retrieval. The scan-start timestamp is retained as the run boundary, but is not used as the per-symbol freshness clock. This preserves the strict future-tick rejection while preventing request duration from manufacturing a future-tick result.

The current MetaQuotes-Demo observation measured a stable two-hour offset: raw tick and M1 candle epochs both converted to UTC were two hours ahead of the Python UTC clock, while Windows local time was UTC+08:00. This is recorded as an auditable project default; the CLI override remains available for a separately verified terminal context. Scan records persist the offset, configuration state, and source used for interpretation.

Financial history and position timestamps use the same boundary rule. The original MT5 broker label is retained in SQLite and exposed as `*_raw`; the audited server offset is applied once to produce `*_utc`; the dashboard/API `*_at` display value is then rendered in Malaysia time (`Asia/Kuala_Lumpur`, UTC+8). Python capture timestamps remain UTC with an explicit Malaysia display companion. An unconfigured broker never receives a silent timestamp conversion.

The Storage Manager is a dry-run-only project health and retention report. It reports disk thresholds at 70%, 85%, and 90%, protects the database and application files, and classifies temporary, diagnostic, archive, and unclassified files for review. `python -m live_trade storage` and the dashboard Storage Manager page never delete files; every scan is appended to the storage audit log.

## Stage 1.7 historical data foundation

`instrument_intelligence.py` classifies the broker's full `symbols_get()` universe from broker-provided path, category, description, currencies, contract specifications, quote state, spread, and trade mode. It distinguishes research suitability from current market availability: a closed market or missing current quote is not permanently treated as unavailable. Classifications are refreshable and persist in `instrument_catalog`.

`historical.py` provides the historical coverage audit, OHLC/timestamp validation, priority-based collection planning, and bounded resumable collection engine. Coverage is stored per symbol/timeframe in `historical_coverage`; accepted or flagged historical candles use a composite `(symbol, timeframe, raw_time)` key in `historical_candles`, with both raw and effective timestamps retained. Collection jobs and events persist progress, retries, errors, pause/resume/cancel state, and next MT5 position. Repeating a completed plan performs no new batch and SQLite ignores duplicate candles.

The quality engine validates finite positive OHLC values, candle relationships, duplicates, ordering, future timestamps, missing intervals, and forming latest candles using the central broker-time configuration. Normal Friday-to-Sunday market closures are not reported as data gaps. Readiness is structural (`READY`, `READY_WITH_WARNINGS`, `INCOMPLETE`, `INVALID`, `INSUFFICIENT_HISTORY`, or `NOT_COLLECTED`) and never implies profitability.

The collection scheduler is deliberately persistence-backed rather than an OS-level daemon. `collection-status` reports `active_scheduler=false` unless an explicit CLI/dashboard collection is running. The dashboard exposes `/api/historical`, audit, plan, bounded-start, pause, resume, and cancel controls; these endpoints have no order or account-mutation capability. Storage protection is checked before each batch, and collection pauses at the 90% disk threshold without deleting anything.

## Stage 1.8 research dataset preparation

Stage 1.8 adds a deterministic instrument-priority model, configurable historical targets, and explicit research-readiness state. The target policy is stored with each collection job: `INITIAL`, `INTERMEDIATE`, and `EXTENDED` levels are defined per supported timeframe. Full-depth audits page through MT5 history in bounded batches and preserve duplicate, ordering, gap, and terminal-call failures as quality evidence; they do not deduplicate or otherwise rewrite broker data during an audit.

The instrument catalog stores priority tier, score, and reason. Tier 1 is capped at a diversified eligible core when the broker offers multiple eligible categories; Tier 2 is a deterministic ranked expansion; Tier 3 remains deferred. If the broker's eligible universe is concentrated in one category, the report records that limitation rather than promoting caution-only symbols into the core. Historical coverage separately records broker candle counts and local persisted counts, so broker availability cannot be mistaken for local research readiness. Provenance includes source status, dataset version, broker-time source, audit scope, target level, and collection job where applicable.

The Stage 1.8 pilot remains bounded and read-only. Collection continues only while explicitly started by the CLI or dashboard, persists progress and retry errors, and pauses at the storage threshold. Higher-timeframe coverage is prioritized before broad M1 expansion; invalid or overlapping MT5 pages remain quarantined for investigation instead of being silently repaired. A `READY_FOR_*` state describes data sufficiency and structural quality only, never profitability or permission to trade.

## Stage 1.8A MT5 paging reliability

The historical collector uses a position request only for the newest bounded page. Older pages are requested by an explicit broker-time range ending strictly before the previous page's oldest raw timestamp. This avoids moving `start_position` boundaries when the current forming candle changes. Pages must be ascending internally, strictly older than their cursor, and disjoint from all earlier pages. Empty, partial, repeated, overlapping, or out-of-order pages remain observable and are not silently repaired.

`collection_batch_diagnostics` is the persisted observability boundary for this process. It records request type and range/position, raw and effective timestamps, returned and accepted counts, forming candles, quality findings, retry count, duration, MT5 error information, result, and provenance. Jobs retain the time cursor, pagination method, attempt timestamps, and last successful timestamp so a queued collection can resume without reusing a moving positional offset. The retry budget is finite and applies only to known transient MT5 error codes.

MT5 raw epoch labels remain preserved as broker-time labels. For the verified MetaQuotes-Demo configuration, effective UTC is calculated once by subtracting UTC+2; live tick and historical candle checks use the same conversion. The collector excludes incomplete current candles from research persistence by default. Existing records are not deleted or rewritten, and a structural `READY` or `READY_FOR_*` state remains distinct from research validity or strategy validation.

## Stage 1.8B controlled expansion

Collection sessions are persisted independently from collection jobs. A session has an explicit symbol/timeframe scope, maximum batches, maximum accepted candles, maximum duration, and maximum SQLite growth. It captures request counts, retries, MT5 failures, retrieved/persisted candles, overlap/duplicate/order/gap/invalid findings, forming candles excluded, final storage, and stop reason. The engine filters execution to the plan's job IDs, so starting a Tier 1 pilot cannot accidentally drain unrelated queued jobs.

The CLI and dashboard use conservative H1/M15 session defaults. Session boundaries are application-level only; no Windows scheduler or continuous restart loop is installed. A successful session means only that its bounded data requests passed the current structural gates. It does not promote a dataset to research-ready status or validate a strategy. Tier 2/Tier 3 and broad M1 collection remain deferred until repeated Tier 1 sessions provide sufficient coverage and reliability evidence.

Stage 1.8C attaches a `ResourceMonitor` to collection sessions. It samples system RAM through `GlobalMemoryStatusEx`, disk usage, SQLite size, and safe CPU/process telemetry where available. Thresholds are explicit: RAM warning/protection/critical at 75/85/90 percent and disk warning/protection/critical at 70/85/90 percent. A protection or critical sample pauses the affected session before another MT5 request. Unavailable fields remain null with a source explanation. Resource snapshots and protection events are persisted with the session so later expansion decisions have operational evidence.

The first-demo reassessment adds a read-only trial-readiness projection. It reads actual historical rows, collection jobs, research experiments, validation outcomes, and strategy-version approvals, then reports a conservative lifecycle stage and explicit blockers. It does not infer execution capability from broker account flags: the read-only MT5 client exposes no order mutation API, so execution and operational risk gates remain blocked until a separately authorized implementation and verification phase.

The minimum execution implementation is isolated in DemoOnlyExecutionAdapter and does not alter ReadOnlyMt5Client. It requires ACCOUNT_TRADE_MODE_DEMO, a demo server identity, connected terminal permissions, a protective stop, broker-compatible symbol volume, spread and risk checks, a unique signal, and post-submit position reconciliation. A rejected, timed-out, or unreconciled order is never reported as filled. The dashboard exposes a separate DEMO_EXECUTION route and panel; the existing DEMO_SHADOW START/STOP behavior is unchanged.

Read-only MT5 initialization uses a finite 10-second timeout so a missing or unreachable terminal cannot leave a scanner or dashboard request hanging indefinitely.

## Stages 1.9–2.1 research intelligence

`research_intelligence.py` is the unified research layer. `generate_hypotheses` creates stable, reproducible definitions for momentum, mean-reversion, breakout, and volatility families. Each hypothesis stores rationale, variables, signal timing, version, and falsification criteria. Experiments reference the existing `historical_candles` table rather than duplicating OHLC data.

`BacktestEngine` is an OHLC-only research simulator. Signals are computed from closed candles and entries occur at the next candle open. Spread, slippage, commission, risk sizing, stop/target assumptions, holding time, trade outcomes, drawdown, expectancy, profit factor, win rate, trade-level Sharpe/Sortino, and an expectancy confidence interval are recorded. Bid/ask history limitations are explicit in the assumptions and are never presented as observed execution data.

`ResearchService` persists the hypothesis → experiment → backtest → validation → learning chain. Validation separates a training segment from an out-of-sample segment and fails candidates for insufficient data, negative expectancy, unmeasurable/low profit factor, or excessive drawdown. Results are classified as `REJECTED` or `INCONCLUSIVE` unless the bounded gates are actually satisfied; no current workflow creates a `VALIDATED_RESEARCH` or execution-approved strategy. Duplicate experiment configurations are prevented by stable IDs and database uniqueness constraints.

Research tables preserve hypotheses, experiments, backtest results, validation runs, strategy-version records, evidence, priorities, learning observations, and reports. The dashboard Research Intelligence page and `/api/research` endpoint expose counts, validation statuses, data readiness, experiments, and learning records. The bounded research control has no MT5 order API and cannot modify approved strategies. The current pilot dataset is not statistically sufficient for a profitability claim.

## Phase 2.2 technical-analysis foundation

`TechnicalAnalyzer` operates only on validated numerical OHLC candles. It calculates SMA 20/50, EMA 9/20/50, RSI 14, MACD 12/26/9, ATR 14, ATR percentage, candle range, and recent volatility change. It detects only rule-based engulfing, hammer, shooting-star, doji, and strong-candle formations with numerical evidence. It also reports measurable higher/lower structure, support/resistance, breakouts, false breakouts, multi-timeframe agreement, and a provisional technical score with factor contributions.

The score is a research measurement, not a probability of profit and not an execution permission. Insufficient history, invalid OHLC values, NaN, and infinite values remain explicit data-quality failures. Technical records are deduplicated by symbol, timeframe, and source-candle timestamp.

Confirmed analysis excludes the current forming MT5 candle by default and records `candle_scope=CLOSED`. Including the current candle requires the explicit `--include-live-candle` option and is labelled `INCLUDING_LIVE`; it is provisional and must not be treated as confirmed pattern evidence.

## Profit Allocation Manager — planned accounting boundary

The future manager is an accounting ledger only. It allocates 20% of eligible cumulative realised net profit to a savings reserve and retains 80% as trading capital. It excludes unrealised P&L, deposits, borrowed funds, and duplicate broker deals. Losses reduce cumulative profitability and must be recovered before new allocations become eligible.

Each allocation will have a unique transaction ID, broker deal reference, timestamp, realised P&L, costs, eligible cumulative profit, savings amount, retained amount, and status. A cumulative high-water mark prevents duplicate allocations.

The dashboard must show trading balance, retained trading capital, reserved savings, and actual withdrawn savings separately. No automatic withdrawal or bank transfer is planned or implemented.
