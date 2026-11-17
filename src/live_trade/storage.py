from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import MarketSnapshot
from .scanner import ScanRun


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
    PRIMARY KEY (scan_id, symbol)
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
                    "INSERT INTO scan_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
