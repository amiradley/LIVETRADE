from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import MarketSnapshot
from .scanner import ScanRun
from .technical import MultiTimeframeAnalysis, TechnicalAnalysis
from .finance import FinancialSnapshot
from .accounting import normalize_deal, savings_preview
from .intelligence import ExchangeRate


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS terminal_snapshots (
    run_id INTEGER PRIMARY KEY REFERENCES runs(run_id),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    run_id INTEGER PRIMARY KEY REFERENCES runs(run_id),
    payload_json TEXT
);
CREATE TABLE IF NOT EXISTS instruments (
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    symbol TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, symbol)
);
CREATE TABLE IF NOT EXISTS ticks (
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    symbol TEXT NOT NULL,
    time TEXT NOT NULL,
    bid REAL NOT NULL,
    ask REAL NOT NULL,
    last REAL,
    volume REAL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, symbol)
);
CREATE TABLE IF NOT EXISTS candles (
    run_id INTEGER NOT NULL REFERENCES runs(run_id),
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    tick_volume INTEGER NOT NULL,
    spread INTEGER,
    real_volume INTEGER,
    PRIMARY KEY (run_id, symbol, timeframe, time)
);
CREATE TABLE IF NOT EXISTS scan_runs (
    scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    discovered_count INTEGER NOT NULL,
    eligible_count INTEGER NOT NULL,
    activated_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_results (
    scan_id INTEGER NOT NULL REFERENCES scan_runs(scan_id),
    symbol TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    tick_time TEXT,
    bid REAL,
    ask REAL,
    spread_points REAL,
    tick_age_seconds REAL,
    session TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    activated INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    candle_counts_json TEXT NOT NULL,
    latest_candle_times_json TEXT NOT NULL,
    effective_tick_time TEXT,
    timestamp_offset_seconds REAL NOT NULL DEFAULT 0,
    timestamp_configured INTEGER NOT NULL DEFAULT 0,
    timestamp_config_source TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scan_id, symbol)
);
CREATE TABLE IF NOT EXISTS technical_analysis (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    analysis_time TEXT NOT NULL,
    source_candle_time TEXT NOT NULL,
    candle_scope TEXT NOT NULL,
    data_quality TEXT NOT NULL,
    indicator_json TEXT NOT NULL,
    patterns_json TEXT NOT NULL,
    structure_json TEXT NOT NULL,
    technical_score REAL,
    score_breakdown_json TEXT NOT NULL,
    UNIQUE (symbol, timeframe, source_candle_time)
);
CREATE TABLE IF NOT EXISTS multi_timeframe_analysis (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    analysis_time TEXT NOT NULL,
    direction TEXT NOT NULL,
    agreement TEXT NOT NULL,
    technical_score REAL,
    score_breakdown_json TEXT NOT NULL,
    timeframe_json TEXT NOT NULL,
    UNIQUE (symbol, analysis_time)
);
CREATE TABLE IF NOT EXISTS financial_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    account_type TEXT NOT NULL,
    account_currency TEXT NOT NULL,
    balance REAL,
    equity REAL,
    margin_used REAL,
    free_margin REAL,
    floating_pnl REAL NOT NULL,
    data_quality TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS financial_positions (
    snapshot_id INTEGER NOT NULL REFERENCES financial_snapshots(snapshot_id),
    ticket INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL,
    entry_price REAL,
    current_price REAL,
    floating_pnl REAL,
    stop_loss REAL,
    take_profit REAL,
    margin REAL,
    opened_at TEXT,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, ticket, symbol)
);
CREATE TABLE IF NOT EXISTS session_records (
    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    starting_balance REAL,
    starting_equity REAL,
    ending_balance REAL,
    ending_equity REAL,
    message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS broker_deals (
    deal_id INTEGER PRIMARY KEY,
    order_id INTEGER,
    position_id INTEGER,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_type INTEGER,
    volume REAL,
    price REAL,
    profit REAL,
    commission REAL,
    swap REAL,
    fee REAL,
    deal_time TEXT,
    captured_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_rates (
    rate_id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    rate REAL NOT NULL,
    source TEXT NOT NULL,
    quote_date TEXT,
    retrieved_at TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(base_currency, quote_currency, retrieved_at)
);
CREATE TABLE IF NOT EXISTS savings_allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    calculated_at TEXT NOT NULL,
    cumulative_net_profit REAL NOT NULL,
    prior_high_water_mark REAL NOT NULL,
    eligible_profit REAL NOT NULL,
    savings_rate REAL NOT NULL,
    savings_amount REAL NOT NULL,
    retained_amount REAL NOT NULL,
    source_deal_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    calculation_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instrument_catalog (
    symbol TEXT PRIMARY KEY,
    refreshed_at TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT '',
    currency_base TEXT,
    currency_profit TEXT,
    currency_margin TEXT,
    contract_size REAL,
    tick_size REAL,
    tick_value REAL,
    volume_min REAL,
    volume_max REAL,
    volume_step REAL,
    trade_mode INTEGER,
    visible INTEGER,
    bid REAL,
    ask REAL,
    spread_points REAL,
    market_state TEXT NOT NULL,
    trading_status TEXT NOT NULL,
    suitability TEXT NOT NULL,
    suitability_reasons_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instrument_catalog_suitability ON instrument_catalog(suitability);
CREATE INDEX IF NOT EXISTS idx_instrument_catalog_category ON instrument_catalog(category);
CREATE TABLE IF NOT EXISTS historical_coverage (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    audited_at TEXT NOT NULL,
    earliest_candle_time TEXT,
    latest_candle_time TEXT,
    candle_count INTEGER NOT NULL,
    complete_candle_count INTEGER NOT NULL,
    gap_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    invalid_count INTEGER NOT NULL,
    timestamp_integrity TEXT NOT NULL,
    latest_candle_complete INTEGER NOT NULL,
    data_quality TEXT NOT NULL,
    readiness TEXT NOT NULL,
    error TEXT,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe)
);
CREATE INDEX IF NOT EXISTS idx_historical_coverage_readiness ON historical_coverage(readiness);
CREATE TABLE IF NOT EXISTS historical_candles (
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    raw_time TEXT NOT NULL,
    effective_time TEXT NOT NULL,
    open REAL NOT NULL,
    high REAL NOT NULL,
    low REAL NOT NULL,
    close REAL NOT NULL,
    tick_volume INTEGER NOT NULL,
    spread INTEGER,
    real_volume INTEGER,
    quality_status TEXT NOT NULL,
    quality_reasons_json TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    PRIMARY KEY (symbol, timeframe, raw_time)
);
CREATE INDEX IF NOT EXISTS idx_historical_candles_range ON historical_candles(symbol, timeframe, effective_time);
CREATE TABLE IF NOT EXISTS collection_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    tier TEXT NOT NULL,
    priority REAL NOT NULL,
    target_bars INTEGER NOT NULL,
    downloaded_bars INTEGER NOT NULL DEFAULT 0,
    next_start_position INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(symbol, timeframe, tier)
);
CREATE INDEX IF NOT EXISTS idx_collection_jobs_status_priority ON collection_jobs(status, priority DESC);
CREATE TABLE IF NOT EXISTS collection_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER,
    event_time TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    rationale TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    version TEXT NOT NULL,
    status TEXT NOT NULL,
    falsification_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS research_experiments (
    experiment_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL REFERENCES research_hypotheses(hypothesis_id),
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    data_start TEXT,
    data_end TEXT,
    config_json TEXT NOT NULL,
    data_reference_json TEXT NOT NULL,
    priority REAL NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(hypothesis_id, symbol, timeframe, config_json, data_reference_json)
);
CREATE INDEX IF NOT EXISTS idx_research_experiments_status_priority ON research_experiments(status, priority DESC);
CREATE TABLE IF NOT EXISTS backtest_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
    run_key TEXT NOT NULL UNIQUE,
    metrics_json TEXT NOT NULL,
    trades_json TEXT NOT NULL,
    assumptions_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_runs (
    validation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES research_experiments(experiment_id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    failure_reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id TEXT NOT NULL,
    version TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL REFERENCES research_hypotheses(hypothesis_id),
    parent_version TEXT,
    status TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(strategy_id, version)
);
CREATE TABLE IF NOT EXISTS learning_observations (
    observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    subject TEXT NOT NULL,
    observation TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    confidence_label TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT,
    evidence_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    source_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_priorities (
    priority_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    priority REAL NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_reports (
    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class MarketDataStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        existing_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(scan_results)")
        }
        if "effective_tick_time" not in existing_columns:
            connection.execute("ALTER TABLE scan_results ADD COLUMN effective_tick_time TEXT")
        if "timestamp_offset_seconds" not in existing_columns:
            connection.execute(
                "ALTER TABLE scan_results ADD COLUMN timestamp_offset_seconds REAL NOT NULL DEFAULT 0"
            )
        if "timestamp_configured" not in existing_columns:
            connection.execute("ALTER TABLE scan_results ADD COLUMN timestamp_configured INTEGER NOT NULL DEFAULT 0")
        if "timestamp_config_source" not in existing_columns:
            connection.execute("ALTER TABLE scan_results ADD COLUMN timestamp_config_source TEXT NOT NULL DEFAULT ''")
        technical_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(technical_analysis)")
        }
        if "candle_scope" not in technical_columns:
            connection.execute(
                "ALTER TABLE technical_analysis ADD COLUMN candle_scope TEXT NOT NULL DEFAULT 'UNKNOWN'"
            )
        return connection

    def save_snapshot(self, snapshot: MarketSnapshot) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO runs (captured_at) VALUES (?)",
                (snapshot.captured_at.isoformat(),),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO terminal_snapshots VALUES (?, ?)",
                (run_id, json.dumps(dict(snapshot.terminal_info), default=str)),
            )
            account_payload = (
                json.dumps(dict(snapshot.account_info), default=str)
                if snapshot.account_info is not None
                else None
            )
            connection.execute(
                "INSERT INTO account_snapshots VALUES (?, ?)",
                (run_id, account_payload),
            )
            for instrument in snapshot.instruments:
                symbol = str(instrument.get("name", instrument.get("symbol", "UNKNOWN")))
                connection.execute(
                    "INSERT OR REPLACE INTO instruments VALUES (?, ?, ?)",
                    (run_id, symbol, json.dumps(dict(instrument), default=str)),
                )
            for tick in snapshot.ticks:
                connection.execute(
                    "INSERT INTO ticks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        tick.symbol,
                        tick.time.isoformat(),
                        tick.bid,
                        tick.ask,
                        tick.last,
                        tick.volume,
                        json.dumps(dict(tick.raw), default=str),
                    ),
                )
            for candle in snapshot.candles:
                connection.execute(
                    "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        candle.symbol,
                        candle.timeframe,
                        candle.time.isoformat(),
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.tick_volume,
                        candle.spread,
                        candle.real_volume,
                    ),
                )
            return run_id

    def counts(self) -> dict[str, int]:
        with self.connect() as connection:
            tables = ("runs", "terminal_snapshots", "account_snapshots", "instruments", "ticks", "candles")
            return {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }

    def save_scan(self, scan: ScanRun) -> int:
        eligible_count = sum(result.eligible for result in scan.results)
        activated_count = sum(result.activated for result in scan.results)
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO scan_runs "
                "(started_at, completed_at, discovered_count, eligible_count, activated_count) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    scan.started_at.isoformat(),
                    scan.completed_at.isoformat(),
                    scan.discovered_count,
                    eligible_count,
                    activated_count,
                ),
            )
            scan_id = int(cursor.lastrowid)
            for result in scan.results:
                connection.execute(
                    "INSERT INTO scan_results "
                    "(scan_id, symbol, captured_at, tick_time, bid, ask, spread_points, "
                    "tick_age_seconds, session, eligible, activated, reasons_json, "
                    "candle_counts_json, latest_candle_times_json, effective_tick_time, "
                    "timestamp_offset_seconds, timestamp_configured, timestamp_config_source) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        scan_id,
                        result.symbol,
                        result.captured_at.isoformat(),
                        result.tick_time.isoformat() if result.tick_time else None,
                        result.bid,
                        result.ask,
                        result.spread_points,
                        result.tick_age_seconds,
                        result.session,
                        int(result.eligible),
                        int(result.activated),
                        json.dumps(result.reasons),
                        json.dumps(dict(result.candle_counts)),
                        json.dumps(
                            {key: value.isoformat() for key, value in result.latest_candle_times.items()}
                        ),
                        result.effective_tick_time.isoformat() if result.effective_tick_time else None,
                        result.timestamp_offset_seconds,
                        int(result.timestamp_configured),
                        result.timestamp_config_source,
                    ),
                )
            return scan_id

    def scan_counts(self, scan_id: int) -> dict[str, int]:
        with self.connect() as connection:
            run = connection.execute(
                "SELECT discovered_count, eligible_count, activated_count "
                "FROM scan_runs WHERE scan_id = ?",
                (scan_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"Unknown scan_id {scan_id}")
            result_count = connection.execute(
                "SELECT COUNT(*) FROM scan_results WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
            return {
                "discovered_count": int(run[0]),
                "eligible_count": int(run[1]),
                "activated_count": int(run[2]),
                "result_count": int(result_count),
            }

    def save_technical_analysis(self, analysis: TechnicalAnalysis) -> int:
        payload = analysis.to_dict()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO technical_analysis "
                "(symbol, timeframe, analysis_time, source_candle_time, candle_scope, data_quality, "
                "indicator_json, patterns_json, structure_json, technical_score, score_breakdown_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, timeframe, source_candle_time) DO UPDATE SET "
                "analysis_time = excluded.analysis_time, "
                "candle_scope = excluded.candle_scope, "
                "data_quality = excluded.data_quality, "
                "indicator_json = excluded.indicator_json, "
                "patterns_json = excluded.patterns_json, "
                "structure_json = excluded.structure_json, "
                "technical_score = excluded.technical_score, "
                "score_breakdown_json = excluded.score_breakdown_json",
                (
                    analysis.symbol,
                    analysis.timeframe,
                    analysis.analysis_time.isoformat(),
                    analysis.source_candle_time.isoformat(),
                    analysis.candle_scope,
                    analysis.data_quality,
                    json.dumps(payload["indicator_values"], default=str),
                    json.dumps(payload["patterns"], default=str),
                    json.dumps(payload["structure"], default=str),
                    analysis.technical_score,
                    json.dumps(payload["score_breakdown"], default=str),
                ),
            )
            row = connection.execute(
                "SELECT analysis_id FROM technical_analysis "
                "WHERE symbol = ? AND timeframe = ? AND source_candle_time = ?",
                (analysis.symbol, analysis.timeframe, analysis.source_candle_time.isoformat()),
            ).fetchone()
            assert row is not None
            return int(row[0])

    def save_multi_timeframe_analysis(self, analysis: MultiTimeframeAnalysis) -> int:
        payload = analysis.to_dict()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO multi_timeframe_analysis "
                "(symbol, analysis_time, direction, agreement, technical_score, "
                "score_breakdown_json, timeframe_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    analysis.symbol,
                    analysis.analysis_time.isoformat(),
                    analysis.direction,
                    analysis.agreement,
                    analysis.technical_score,
                    json.dumps(payload["score_breakdown"], default=str),
                    json.dumps(payload["timeframes"], default=str),
                ),
            )
            row = connection.execute(
                "SELECT analysis_id FROM multi_timeframe_analysis "
                "WHERE symbol = ? AND analysis_time = ?",
                (analysis.symbol, analysis.analysis_time.isoformat()),
            ).fetchone()
            assert row is not None
            return int(row[0])

    def save_financial_snapshot(self, snapshot: FinancialSnapshot) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO financial_snapshots "
                "(captured_at, account_type, account_currency, balance, equity, margin_used, "
                "free_margin, floating_pnl, data_quality) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.captured_at.isoformat(),
                    snapshot.account_type,
                    snapshot.account_currency,
                    snapshot.balance,
                    snapshot.equity,
                    snapshot.margin_used,
                    snapshot.free_margin,
                    snapshot.floating_pnl,
                    snapshot.data_quality,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for position in snapshot.positions:
                connection.execute(
                    "INSERT INTO financial_positions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        position.ticket,
                        position.symbol,
                        position.side,
                        position.volume,
                        position.entry_price,
                        position.current_price,
                        position.floating_pnl,
                        position.stop_loss,
                        position.take_profit,
                        position.margin,
                        position.opened_at.isoformat() if position.opened_at else None,
                        position.captured_at.isoformat(),
                        json.dumps(dict(position.raw), default=str),
                    ),
                )
            return snapshot_id

    def create_session(
        self,
        *,
        mode: str,
        state: str,
        started_at: Any,
        starting_balance: float | None,
        starting_equity: float | None,
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO session_records "
                "(mode, state, started_at, starting_balance, starting_equity) VALUES (?, ?, ?, ?, ?)",
                (mode, state, started_at.isoformat(), starting_balance, starting_equity),
            )
            return int(cursor.lastrowid)

    def update_session(
        self,
        session_id: int | None,
        *,
        state: str,
        ending_balance: float | None,
        ending_equity: float | None,
        stopped_at: Any | None,
        message: str,
    ) -> None:
        if session_id is None:
            raise KeyError("Cannot update a session without a session_id")
        with self.connect() as connection:
            connection.execute(
                "UPDATE session_records SET state = ?, stopped_at = ?, ending_balance = ?, "
                "ending_equity = ?, message = ? WHERE session_id = ?",
                (
                    state,
                    stopped_at.isoformat() if stopped_at else None,
                    ending_balance,
                    ending_equity,
                    message,
                    session_id,
                ),
            )

    def save_broker_deals(self, deals: Any, captured_at: Any) -> int:
        saved = 0
        with self.connect() as connection:
            for raw in deals:
                deal = normalize_deal(raw)
                if deal is None:
                    continue
                connection.execute(
                    "INSERT OR IGNORE INTO broker_deals "
                    "(deal_id, order_id, position_id, symbol, side, entry_type, volume, price, "
                    "profit, commission, swap, fee, deal_time, captured_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        deal.deal_id,
                        deal.order_id,
                        deal.position_id,
                        deal.symbol,
                        deal.side,
                        deal.entry_type,
                        deal.volume,
                        deal.price,
                        deal.profit,
                        deal.commission,
                        deal.swap,
                        deal.fee,
                        deal.deal_time.isoformat() if deal.deal_time else None,
                        captured_at.isoformat(),
                        json.dumps(dict(deal.raw), default=str),
                    ),
                )
                saved += 1
        return saved

    def save_exchange_rate(self, rate: ExchangeRate) -> int | None:
        if rate.rate is None or rate.retrieved_at is None:
            return None
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO exchange_rates "
                "(base_currency, quote_currency, rate, source, quote_date, retrieved_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    rate.base_currency,
                    rate.quote_currency,
                    rate.rate,
                    rate.source or "unknown",
                    rate.quote_date.isoformat() if rate.quote_date else None,
                    rate.retrieved_at.isoformat(),
                    rate.status,
                ),
            )
            return int(cursor.lastrowid) if cursor.lastrowid else None

    def apply_savings_allocation(
        self, cumulative_net_profit: float, source_deal_count: int, calculated_at: Any
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT prior_high_water_mark, cumulative_net_profit FROM savings_allocations "
                "ORDER BY allocation_id DESC LIMIT 1"
            ).fetchone()
            prior_high_water_mark = float(row[1]) if row else 0.0
            preview = savings_preview(cumulative_net_profit, prior_high_water_mark)
            totals = connection.execute(
                "SELECT COALESCE(SUM(savings_amount), 0), COALESCE(SUM(retained_amount), 0) "
                "FROM savings_allocations"
            ).fetchone()
            total_reserved = float(totals[0])
            total_retained = float(totals[1])
            allocation_id = None
            if preview["status"] == "ALLOCATED":
                cursor = connection.execute(
                    "INSERT INTO savings_allocations "
                    "(calculated_at, cumulative_net_profit, prior_high_water_mark, eligible_profit, "
                    "savings_rate, savings_amount, retained_amount, source_deal_count, status, calculation_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        calculated_at.isoformat(),
                        preview["cumulative_net_profit"],
                        preview["prior_high_water_mark"],
                        preview["eligible_profit"],
                        preview["savings_rate"],
                        preview["savings_amount"],
                        preview["retained_amount"],
                        source_deal_count,
                        preview["status"],
                        json.dumps(preview),
                    ),
                )
                allocation_id = int(cursor.lastrowid)
                total_reserved += float(preview["savings_amount"])
                total_retained += float(preview["retained_amount"])
            return {
                **preview,
                "allocation_id": allocation_id,
                "total_reserved": total_reserved,
                "total_retained": total_retained,
            }

    def save_instrument_classifications(self, records: Any) -> int:
        saved = 0
        with self.connect() as connection:
            for record in records:
                connection.execute(
                    "INSERT INTO instrument_catalog ("
                    "symbol, refreshed_at, category, description, path, exchange, currency_base, "
                    "currency_profit, currency_margin, contract_size, tick_size, tick_value, "
                    "volume_min, volume_max, volume_step, trade_mode, visible, bid, ask, "
                    "spread_points, market_state, trading_status, suitability, suitability_reasons_json, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(symbol) DO UPDATE SET refreshed_at=excluded.refreshed_at, "
                    "category=excluded.category, description=excluded.description, path=excluded.path, "
                    "exchange=excluded.exchange, currency_base=excluded.currency_base, "
                    "currency_profit=excluded.currency_profit, currency_margin=excluded.currency_margin, "
                    "contract_size=excluded.contract_size, tick_size=excluded.tick_size, tick_value=excluded.tick_value, "
                    "volume_min=excluded.volume_min, volume_max=excluded.volume_max, volume_step=excluded.volume_step, "
                    "trade_mode=excluded.trade_mode, visible=excluded.visible, bid=excluded.bid, ask=excluded.ask, "
                    "spread_points=excluded.spread_points, market_state=excluded.market_state, "
                    "trading_status=excluded.trading_status, suitability=excluded.suitability, "
                    "suitability_reasons_json=excluded.suitability_reasons_json, payload_json=excluded.payload_json",
                    (
                        record["symbol"], record["refreshed_at"], record["category"], record.get("description", ""),
                        record.get("path", ""), record.get("exchange", ""), record.get("currency_base"),
                        record.get("currency_profit"), record.get("currency_margin"), record.get("contract_size"),
                        record.get("tick_size"), record.get("tick_value"), record.get("volume_min"),
                        record.get("volume_max"), record.get("volume_step"), record.get("trade_mode"),
                        int(bool(record.get("visible"))) if record.get("visible") is not None else None,
                        record.get("bid"), record.get("ask"), record.get("spread_points"),
                        record["market_state"], record["trading_status"], record["suitability"],
                        json.dumps(record.get("suitability_reasons", [])), json.dumps(record.get("raw", {}), default=str),
                    ),
                )
                saved += 1
        return saved

    def instrument_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT suitability, COUNT(*) FROM instrument_catalog GROUP BY suitability"
            ).fetchall()
            categories = connection.execute(
                "SELECT category, COUNT(*) FROM instrument_catalog GROUP BY category ORDER BY category"
            ).fetchall()
            return {
                "total": int(connection.execute("SELECT COUNT(*) FROM instrument_catalog").fetchone()[0]),
                "by_suitability": {str(row[0]): int(row[1]) for row in rows},
                "by_category": {str(row[0]): int(row[1]) for row in categories},
                "refreshed_at": connection.execute("SELECT MAX(refreshed_at) FROM instrument_catalog").fetchone()[0],
            }

    def save_coverage(self, report: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO historical_coverage (symbol, timeframe, audited_at, earliest_candle_time, "
                "latest_candle_time, candle_count, complete_candle_count, gap_count, duplicate_count, "
                "invalid_count, timestamp_integrity, latest_candle_complete, data_quality, readiness, error, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, timeframe) DO UPDATE SET audited_at=excluded.audited_at, "
                "earliest_candle_time=excluded.earliest_candle_time, latest_candle_time=excluded.latest_candle_time, "
                "candle_count=excluded.candle_count, complete_candle_count=excluded.complete_candle_count, "
                "gap_count=excluded.gap_count, duplicate_count=excluded.duplicate_count, invalid_count=excluded.invalid_count, "
                "timestamp_integrity=excluded.timestamp_integrity, latest_candle_complete=excluded.latest_candle_complete, "
                "data_quality=excluded.data_quality, readiness=excluded.readiness, error=excluded.error, metadata_json=excluded.metadata_json",
                (
                    report["symbol"], report["timeframe"], report["audited_at"], report.get("earliest_candle_time"),
                    report.get("latest_candle_time"), report["candle_count"], report["complete_candle_count"],
                    report["gap_count"], report["duplicate_count"], report["invalid_count"], report["timestamp_integrity"],
                    int(report["latest_candle_complete"]), report["data_quality"], report["readiness"],
                    report.get("error"), json.dumps(report.get("metadata", {}), default=str),
                ),
            )

    def coverage_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT timeframe, COUNT(*), SUM(candle_count), SUM(complete_candle_count) "
                "FROM historical_coverage GROUP BY timeframe ORDER BY timeframe"
            ).fetchall()
            readiness = connection.execute(
                "SELECT readiness, COUNT(*) FROM historical_coverage GROUP BY readiness"
            ).fetchall()
            return {
                "by_timeframe": {
                    str(row[0]): {"audited": int(row[1]), "candles": int(row[2] or 0), "complete": int(row[3] or 0)}
                    for row in rows
                },
                "by_readiness": {str(row[0]): int(row[1]) for row in readiness},
            }

    def save_historical_candles(self, records: Any) -> int:
        saved = 0
        with self.connect() as connection:
            for record in records:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO historical_candles ("
                    "symbol, timeframe, raw_time, effective_time, open, high, low, close, tick_volume, spread, real_volume, "
                    "quality_status, quality_reasons_json, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record["symbol"], record["timeframe"], record["raw_time"], record["effective_time"],
                        record["open"], record["high"], record["low"], record["close"], record["tick_volume"],
                        record.get("spread"), record.get("real_volume"), record["quality_status"],
                        json.dumps(record.get("quality_reasons", [])), record["collected_at"],
                    ),
                )
                saved += max(0, cursor.rowcount)
        return saved

    def historical_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            return {
                "candles": int(connection.execute("SELECT COUNT(*) FROM historical_candles").fetchone()[0]),
                "symbols": int(connection.execute("SELECT COUNT(DISTINCT symbol) FROM historical_candles").fetchone()[0]),
                "bytes_estimate": int(connection.execute("SELECT COUNT(*) FROM historical_candles").fetchone()[0]) * 96,
                "quality": {
                    str(row[0]): int(row[1])
                    for row in connection.execute("SELECT quality_status, COUNT(*) FROM historical_candles GROUP BY quality_status")
                },
            }

    def create_collection_jobs(self, items: Any, now: Any) -> list[int]:
        job_ids: list[int] = []
        with self.connect() as connection:
            for item in items:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO collection_jobs "
                    "(symbol, timeframe, tier, priority, target_bars, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, ?)",
                    (item["symbol"], item["timeframe"], item["tier"], item["priority"], item["target_bars"], now.isoformat(), now.isoformat()),
                )
                if cursor.lastrowid:
                    job_ids.append(int(cursor.lastrowid))
                else:
                    row = connection.execute(
                        "SELECT job_id FROM collection_jobs WHERE symbol=? AND timeframe=? AND tier=?",
                        (item["symbol"], item["timeframe"], item["tier"]),
                    ).fetchone()
                    if row:
                        job_ids.append(int(row[0]))
        return job_ids

    def collection_jobs(self, statuses: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = connection.execute(
                    f"SELECT * FROM collection_jobs WHERE status IN ({placeholders}) ORDER BY priority DESC, job_id",
                    statuses,
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM collection_jobs ORDER BY priority DESC, job_id").fetchall()
            return [dict(row) for row in rows]

    def update_collection_job(self, job_id: int, now: Any, **fields: Any) -> None:
        allowed = {"downloaded_bars", "next_start_position", "status", "attempts", "last_error"}
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = now.isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE collection_jobs SET {assignments} WHERE job_id = ?",
                (*values.values(), job_id),
            )

    def collection_event(self, job_id: int | None, event_type: str, message: str, now: Any, payload: Any = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO collection_events (job_id, event_time, event_type, message, payload_json) VALUES (?, ?, ?, ?, ?)",
                (job_id, now.isoformat(), event_type, message, json.dumps(payload or {}, default=str)),
            )

    def load_historical_candles(self, symbol: str, timeframe: str, limit: int = 10000) -> list[Any]:
        from datetime import datetime
        from .models import Candle

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT raw_time, open, high, low, close, tick_volume, spread, real_volume "
                "FROM historical_candles WHERE symbol=? AND timeframe=? ORDER BY effective_time LIMIT ?",
                (symbol.upper(), timeframe.upper(), limit),
            ).fetchall()
        return [
            Candle(symbol.upper(), timeframe.upper(), datetime.fromisoformat(row[0]), row[1], row[2], row[3], row[4], row[5], row[6], row[7])
            for row in rows
        ]

    def research_save(self, table: str, payload: dict[str, Any]) -> None:
        import json as _json

        with self.connect() as connection:
            if table == "hypothesis":
                connection.execute(
                    "INSERT OR IGNORE INTO research_hypotheses "
                    "(hypothesis_id,name,family,rationale,definition_json,version,status,falsification_json,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", tuple(payload[key] for key in ("hypothesis_id", "name", "family", "rationale", "definition_json", "version", "status", "falsification_json", "created_at"))
                )
            elif table == "experiment":
                connection.execute(
                    "INSERT OR IGNORE INTO research_experiments "
                    "(experiment_id,hypothesis_id,symbol,timeframe,data_start,data_end,config_json,data_reference_json,priority,status,progress,failure_reason,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(payload.get(key) for key in ("experiment_id", "hypothesis_id", "symbol", "timeframe", "data_start", "data_end", "config_json", "data_reference_json", "priority", "status", "progress", "failure_reason", "created_at", "updated_at"))
                )
            elif table == "backtest":
                connection.execute(
                    "INSERT OR IGNORE INTO backtest_results (experiment_id,run_key,metrics_json,trades_json,assumptions_json,created_at) VALUES (?,?,?,?,?,?)",
                    tuple(payload[key] for key in ("experiment_id", "run_key", "metrics_json", "trades_json", "assumptions_json", "created_at")),
                )
            elif table == "validation":
                connection.execute(
                    "INSERT INTO validation_runs (experiment_id,stage,status,metrics_json,failure_reasons_json,created_at) VALUES (?,?,?,?,?,?)",
                    tuple(payload[key] for key in ("experiment_id", "stage", "status", "metrics_json", "failure_reasons_json", "created_at")),
                )
            elif table == "learning":
                connection.execute(
                    "INSERT INTO learning_observations (category,subject,observation,evidence_json,confidence_label,created_at) VALUES (?,?,?,?,?,?)",
                    tuple(payload[key] for key in ("category", "subject", "observation", "evidence_json", "confidence_label", "created_at")),
                )
            elif table == "evidence":
                connection.execute(
                    "INSERT INTO research_evidence (experiment_id,evidence_type,statement,source_json,created_at) VALUES (?,?,?,?,?)",
                    tuple(payload.get(key) for key in ("experiment_id", "evidence_type", "statement", "source_json", "created_at")),
                )
            elif table == "priority":
                connection.execute(
                    "INSERT INTO research_priorities (subject,priority,reason,status,created_at) VALUES (?,?,?,?,?)",
                    tuple(payload[key] for key in ("subject", "priority", "reason", "status", "created_at")),
                )
            elif table == "report":
                connection.execute(
                    "INSERT INTO research_reports (report_date,report_json,created_at) VALUES (?,?,?)",
                    tuple(payload[key] for key in ("report_date", "report_json", "created_at")),
                )
            else:
                raise ValueError(f"Unknown research table: {table}")

    def research_update_experiment(self, experiment_id: str, status: str, progress: float, failure_reason: str | None = None) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE research_experiments SET status=?, progress=?, failure_reason=?, updated_at=datetime('now') WHERE experiment_id=?",
                (status, progress, failure_reason, experiment_id),
            )

    def research_rows(self, table: str, limit: int = 100) -> list[dict[str, Any]]:
        allowed = {"research_hypotheses", "research_experiments", "backtest_results", "validation_runs", "learning_observations", "strategy_versions", "research_evidence", "research_priorities"}
        if table not in allowed:
            raise ValueError(f"Unknown research table: {table}")
        with self.connect() as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [dict(row) for row in rows]

    def research_summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            def count(table: str, where: str = "", params: tuple[Any, ...] = ()) -> int:
                return int(connection.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])
            return {
                "hypotheses": count("research_hypotheses"),
                "experiments": count("research_experiments"),
                "completed": count("research_experiments", "WHERE status='COMPLETE'"),
                "running": count("research_experiments", "WHERE status='RUNNING'"),
                "queued": count("research_experiments", "WHERE status='QUEUED'"),
                "failed": count("research_experiments", "WHERE status='FAILED'"),
                "validation_runs": count("validation_runs"),
                "learning_observations": count("learning_observations"),
                "evidence_records": count("research_evidence"),
                "by_validation_status": {
                    str(row[0]): int(row[1]) for row in connection.execute("SELECT status, COUNT(*) FROM validation_runs GROUP BY status")
                },
            }
