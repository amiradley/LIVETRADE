from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from live_trade.models import Candle, MarketSnapshot, Tick
from live_trade.scanner import ScanResult, ScanRun
from live_trade.storage import MarketDataStore
from live_trade.technical import TechnicalAnalyzer


def test_snapshot_is_persisted(tmp_path):
    snapshot = MarketSnapshot(
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        terminal_info={"name": "Fake MT5"},
        account_info={"login": 12345},
        instruments=({"name": "EURUSD"},),
        ticks=(
            Tick(
                symbol="EURUSD",
                time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                bid=1.1,
                ask=1.1002,
                last=1.1001,
                volume=10,
                raw={"bid": 1.1},
            ),
        ),
        candles=(
            Candle(
                symbol="EURUSD",
                timeframe="M1",
                time=datetime(2026, 1, 1, tzinfo=timezone.utc),
                open=1.1,
                high=1.101,
                low=1.099,
                close=1.1005,
                tick_volume=42,
            ),
        ),
    )
    store = MarketDataStore(tmp_path / "market.sqlite3")
    run_id = store.save_snapshot(snapshot)

    assert run_id == 1
    assert store.counts() == {
        "runs": 1,
        "terminal_snapshots": 1,
        "account_snapshots": 1,
        "instruments": 1,
        "ticks": 1,
        "candles": 1,
    }


def test_scan_is_persisted(tmp_path):
    captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scan = ScanRun(
        started_at=captured_at,
        completed_at=captured_at,
        discovered_count=2,
        results=(
            ScanResult(
                symbol="EURUSD",
                captured_at=captured_at,
                tick_time=captured_at,
                bid=1.1,
                ask=1.1002,
                spread_points=20.0,
                tick_age_seconds=0.0,
                session="ASIA_PACIFIC",
                eligible=True,
                activated=True,
                candle_counts={"M1": 1},
                latest_candle_times={"M1": captured_at},
            ),
        ),
    )
    store = MarketDataStore(tmp_path / "market.sqlite3")

    scan_id = store.save_scan(scan)

    assert store.scan_counts(scan_id) == {
        "discovered_count": 2,
        "eligible_count": 1,
        "activated_count": 1,
        "result_count": 1,
    }


def test_technical_analysis_deduplicates_source_candle(tmp_path):
    candles = tuple(
        Candle(
            symbol="EURUSD",
            timeframe="M1",
            time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
            open=1.0 + index * 0.01,
            high=1.02 + index * 0.01,
            low=0.99 + index * 0.01,
            close=1.01 + index * 0.01,
            tick_volume=100,
        )
        for index in range(60)
    )
    analysis = TechnicalAnalyzer().analyze("EURUSD", "M1", candles)
    store = MarketDataStore(tmp_path / "market.sqlite3")

    first_id = store.save_technical_analysis(analysis)
    second_id = store.save_technical_analysis(analysis)

    assert first_id == second_id


def test_confirmed_analysis_replaces_provisional_record(tmp_path):
    candles = tuple(
        Candle(
            symbol="EURUSD",
            timeframe="M1",
            time=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index),
            open=1.0 + index * 0.01,
            high=1.02 + index * 0.01,
            low=0.99 + index * 0.01,
            close=1.01 + index * 0.01,
            tick_volume=100,
        )
        for index in range(60)
    )
    analysis = TechnicalAnalyzer().analyze("EURUSD", "M1", candles, closed_only=False)
    confirmed = replace(analysis, candle_scope="CLOSED")
    store = MarketDataStore(tmp_path / "market.sqlite3")

    analysis_id = store.save_technical_analysis(analysis)
    confirmed_id = store.save_technical_analysis(confirmed)

    assert analysis_id == confirmed_id
    with store.connect() as connection:
        assert connection.execute(
            "SELECT candle_scope FROM technical_analysis WHERE analysis_id = ?",
            (analysis_id,),
        ).fetchone()[0] == "CLOSED"


def test_sqlite_transaction_rolls_back_after_interrupted_write(tmp_path):
    store = MarketDataStore(tmp_path / "rollback.sqlite3")

    try:
        with store.connect() as connection:
            connection.execute("INSERT INTO runs (captured_at) VALUES (?)", ("interrupted",))
            raise RuntimeError("simulated interrupted transaction")
    except RuntimeError:
        pass

    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
