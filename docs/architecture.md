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

## Phase 2 scanner foundation

`MarketScanner` is a read-only policy layer over the MT5 adapter. It discovers instruments, reads ticks, calculates spread in broker points, rejects stale or invalid data, labels a coarse UTC market session, progressively activates only the lowest-spread eligible candidates, retrieves configured OHLC timeframes, and persists a scan run plus per-symbol results. The scanner does not place orders and does not claim that eligibility implies profitability.

The first scanner increment supports M1, M5, M15, M30, H1, H4, and D1 retrieval and stores candle counts/freshness. Technical indicators, chart-pattern evidence, and multi-timeframe ranking will be added as separate deterministic stages after this data-quality foundation.

Future-dated tick timestamps are intentionally rejected. MT5/broker server clock offsets must be measured and supplied as explicit, audited configuration before any offset-aware activation is allowed; the scanner must not silently reinterpret timestamps.

The current MetaQuotes-Demo observation measured a stable two-hour offset: raw tick and M1 candle epochs both converted to UTC were two hours ahead of the Python UTC clock, while Windows local time was UTC+08:00. The scanner can therefore be run with `--broker-server-offset-hours 2` for this verified terminal context; leaving the option at its default of zero preserves strict rejection until the offset is verified again.

## Profit Allocation Manager — planned accounting boundary

The future manager is an accounting ledger only. It allocates 20% of eligible cumulative realised net profit to a savings reserve and retains 80% as trading capital. It excludes unrealised P&L, deposits, borrowed funds, and duplicate broker deals. Losses reduce cumulative profitability and must be recovered before new allocations become eligible.

Each allocation will have a unique transaction ID, broker deal reference, timestamp, realised P&L, costs, eligible cumulative profit, savings amount, retained amount, and status. A cumulative high-water mark prevents duplicate allocations.

The dashboard must show trading balance, retained trading capital, reserved savings, and actual withdrawn savings separately. No automatic withdrawal or bank transfer is planned or implemented.
