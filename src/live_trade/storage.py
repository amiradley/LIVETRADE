from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import MarketSnapshot
from .scanner import ScanRun
from .technical import MultiTimeframeAnalysis, TechnicalAnalysis
from .finance import FinancialSnapshot


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
                    "timestamp_offset_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
