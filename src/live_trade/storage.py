from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import MarketSnapshot


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
