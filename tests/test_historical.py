from datetime import datetime, timezone
from threading import RLock
from types import SimpleNamespace

from live_trade.historical import CollectionPlanItem, HistoricalDataEngine, target_for, validate_candles
from live_trade.instrument_intelligence import classify_instrument, instrument_category
from live_trade.models import Candle
from live_trade.mt5_client import ReadOnlyMt5Client
from live_trade.storage import MarketDataStore
from live_trade.timestamping import BrokerTimeConfig


def instrument_raw(**overrides):
    raw = {
        "name": "EURUSD", "path": "Forex\\Major", "description": "Euro vs US Dollar", "category": "",
        "trade_mode": 4, "visible": True, "point": 0.00001, "trade_tick_size": 0.00001,
        "trade_contract_size": 100000, "trade_tick_value": 1.0, "volume_min": 0.01, "volume_max": 100,
        "volume_step": 0.01, "bid": 1.1, "ask": 1.1001, "spread": 10,
    }
    raw.update(overrides)
    return raw


def test_instrument_classification_distinguishes_closed_market_from_invalid_data():
    current = classify_instrument(instrument_raw(bid=0, ask=0))
    invalid = classify_instrument(instrument_raw(point=0, trade_contract_size=0))

    assert instrument_category(instrument_raw()) == "FOREX"
    assert current["market_state"] == "NO_CURRENT_QUOTE"
    assert current["suitability"] == "ELIGIBLE_WITH_CAUTION"
    assert invalid["suitability"] == "INSUFFICIENT_DATA"


def test_quality_validator_flags_invalid_ohlc_duplicates_and_future():
    broker_time = BrokerTimeConfig.with_offset(2)
    candles = (
        Candle("EURUSD", "M1", datetime(2026, 1, 1, 2, tzinfo=timezone.utc), 1.1, 1.101, 1.099, 1.100, 10),
        Candle("EURUSD", "M1", datetime(2026, 1, 1, 2, tzinfo=timezone.utc), 1.1, 1.09, 1.099, 1.100, 10),
    )
    report = validate_candles("EURUSD", "M1", candles, broker_time, datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert report.duplicate_count == 1
    assert report.invalid_count == 1
    assert report.readiness == "INVALID"
    assert any(issue["code"] == "duplicate_candle" for issue in report.issues)


def test_quality_validator_does_not_treat_normal_weekend_closure_as_data_gap():
    candles = tuple(
        Candle("EURUSD", "H1", value, 1.1, 1.101, 1.099, 1.100, 10)
        for value in (
            datetime(2026, 1, 9, 21, tzinfo=timezone.utc),
            datetime(2026, 1, 11, 22, tzinfo=timezone.utc),
        )
    )
    report = validate_candles("EURUSD", "H1", candles, BrokerTimeConfig.with_offset(0), datetime(2026, 1, 12, tzinfo=timezone.utc))

    assert report.gap_count == 0


def test_engine_audit_and_collection_are_persisted_and_resumable(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "data" / "market.sqlite3")
    broker_time = BrokerTimeConfig.metaquotes_demo_default()
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    engine = HistoricalDataEngine(client, store, broker_time, tmp_path)
    raw = instrument_raw()
    fake_mt5.symbols_get = lambda: (raw,)

    summary = engine.classify(("EURUSD",))
    reports = engine.audit(("EURUSD",), ("M1",), 1)
    def batch(symbol, timeframe, start, count):
        return [{
            "time": int(datetime(2026, 1, 1, 0, tzinfo=timezone.utc).timestamp()) + start * 60,
            "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 42,
            "spread": 2, "real_volume": 0,
        }]
    fake_mt5.copy_rates_from_pos = batch
    fake_mt5.copy_rates_range = lambda symbol, timeframe, date_from, date_to: [{
        "time": int(date_to.timestamp()), "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005,
        "tick_volume": 42, "spread": 2, "real_volume": 0,
    }]
    plan = [CollectionPlanItem("EURUSD", "M1", "TIER1", 120, 2, 192)]
    result = engine.collect(plan, max_batches=1, batch_size=1, rate_limit_seconds=0)
    second = engine.collect(plan, max_batches=1, batch_size=1, rate_limit_seconds=0)

    assert summary["total"] == 1
    assert summary["by_priority_tier"]["TIER1"] == 1
    assert reports[0].candle_count == 1
    assert result["batches"] == 1
    assert second["historical"]["candles"] == 2
    assert store.historical_summary()["candles"] == 2
    assert store.coverage_summary()["by_timeframe"]["M1"]["audited"] == 1
    assert store.collection_jobs()[0]["downloaded_bars"] == 2


def test_full_depth_audit_pages_older_mt5_batches(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: [{
        "time": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) - start * 60,
        "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 42, "spread": 2, "real_volume": 0,
    }] if start < 3 else []
    range_calls = {"count": 0}
    def range_batch(symbol, timeframe, date_from, date_to):
        range_calls["count"] += 1
        if range_calls["count"] > 2:
            return []
        return [{
            "time": int(date_to.timestamp()), "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005,
            "tick_volume": 42, "spread": 2, "real_volume": 0,
        }]
    fake_mt5.copy_rates_range = range_batch
    engine = HistoricalDataEngine(client, store, BrokerTimeConfig.metaquotes_demo_default(), tmp_path)

    reports = engine.audit(("EURUSD",), ("M1",), bars=1, full_depth=True, max_batches=5)

    assert reports[0].candle_count == 3
    with store.connect() as connection:
        row = connection.execute("SELECT broker_candle_count, source_status, research_readiness FROM historical_coverage").fetchone()
    assert tuple(row) == (3, "AVAILABLE", "NOT_COLLECTED")
    assert target_for("H1", "INITIAL") == 1000


def test_collection_scheduler_persists_pause_resume_cancel(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    now = datetime.now(timezone.utc)
    store.create_collection_jobs([{"symbol": "EURUSD", "timeframe": "M1", "tier": "TIER1", "priority": 1, "target_bars": 10}], now)
    from live_trade.historical import CollectionScheduler

    scheduler = CollectionScheduler(store)
    scheduler.pause()
    assert store.collection_jobs()[0]["status"] == "PAUSED"
    scheduler.resume()
    assert store.collection_jobs()[0]["status"] == "QUEUED"
    scheduler.cancel()
    assert store.collection_jobs()[0]["status"] == "CANCELLED"


def test_time_anchored_paging_rejects_boundary_overlap_and_preserves_evidence(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: [{
        "time": base, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 42,
        "spread": 2, "real_volume": 0,
    }]
    fake_mt5.copy_rates_range = lambda symbol, timeframe, date_from, date_to: [{
        "time": base, "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 42,
        "spread": 2, "real_volume": 0,
    }]
    engine = HistoricalDataEngine(client, store, BrokerTimeConfig.with_offset(0), tmp_path)

    report = engine.audit(("EURUSD",), ("M1",), bars=1, full_depth=True, max_batches=2)[0]

    assert report.data_quality == "ERROR"
    with store.connect() as connection:
        row = connection.execute("SELECT result, overlap_count FROM collection_batch_diagnostics ORDER BY batch_id DESC LIMIT 1").fetchone()
    assert tuple(row) == ("REJECTED", 1)


def test_collection_excludes_forming_candle_and_records_count(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    broker_time = BrokerTimeConfig.with_offset(2)
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    now = datetime.now(timezone.utc)
    complete_effective = now - __import__("datetime").timedelta(minutes=3)
    forming_effective = now - __import__("datetime").timedelta(minutes=0, seconds=10)
    def row(value):
        return {
            "time": int((value + __import__("datetime").timedelta(hours=2)).timestamp()),
            "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 42,
            "spread": 2, "real_volume": 0,
        }
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: [row(complete_effective), row(forming_effective)]
    engine = HistoricalDataEngine(client, store, broker_time, tmp_path)

    result = engine.collect([CollectionPlanItem("EURUSD", "M1", "TIER1", 120, 2, 192)], max_batches=1, batch_size=2, rate_limit_seconds=0)

    assert result["batches"] == 1
    assert store.historical_summary()["candles"] == 1
    diagnostic = store.recent_collection_batch_diagnostics(1)[0]
    assert diagnostic["forming_count"] == 1
    assert diagnostic["accepted_count"] == 1


def test_transient_mt5_failure_retries_and_records_error_evidence(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    calls = {"count": 0}
    def batch(symbol, timeframe, start, count):
        calls["count"] += 1
        if calls["count"] == 1:
            fake_mt5.last_error = lambda: (-1, "Terminal: Call failed")
            return None
        fake_mt5.last_error = lambda: (1, "Success")
        return [{
            "time": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()), "open": 1.1, "high": 1.101,
            "low": 1.099, "close": 1.1005, "tick_volume": 42, "spread": 2, "real_volume": 0,
        }]
    fake_mt5.copy_rates_from_pos = batch
    engine = HistoricalDataEngine(client, store, BrokerTimeConfig.with_offset(0), tmp_path)

    result = engine.collect([CollectionPlanItem("EURUSD", "M1", "TIER1", 120, 1, 96)], max_batches=1, batch_size=1, rate_limit_seconds=0)

    assert result["batches"] == 1
    diagnostic = store.recent_collection_batch_diagnostics(1)[0]
    assert diagnostic["retry_count"] == 1
    assert calls["count"] == 2


def test_collection_session_is_bounded_and_does_not_process_unplanned_queue_jobs(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: [{
        "time": int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) - start * 60,
        "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005, "tick_volume": 42,
        "spread": 2, "real_volume": 0,
    }]
    store.create_collection_jobs([{"symbol": "GBPUSD", "timeframe": "H1", "tier": "TIER1", "priority": 100, "target_bars": 10}], datetime.now(timezone.utc))
    engine = HistoricalDataEngine(client, store, BrokerTimeConfig.with_offset(0), tmp_path)

    result = engine.collect_session(
        [CollectionPlanItem("EURUSD", "H1", "TIER1", 120, 2, 192)],
        max_batches=1,
        batch_size=2,
        max_total_candles=1,
        max_duration_seconds=30,
        max_storage_growth_bytes=1024 * 1024,
        rate_limit_seconds=0,
    )

    assert result["session"]["status"] == "COMPLETED"
    assert result["session"]["candles_persisted"] == 1
    jobs = {row["symbol"]: row for row in store.collection_jobs()}
    assert jobs["GBPUSD"]["status"] == "QUEUED"
    assert jobs["EURUSD"]["downloaded_bars"] == 1
    assert len(store.collection_sessions()) == 1


def test_collection_session_stops_at_storage_protection(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    engine = HistoricalDataEngine(client, store, BrokerTimeConfig.with_offset(0), tmp_path)
    engine.storage_manager.scan = lambda: SimpleNamespace(disk_used_percent=90.0, sqlite_size_bytes=100, to_dict=lambda: {"health_status": "PROTECTED"})

    result = engine.collect_session(
        [CollectionPlanItem("EURUSD", "H1", "TIER1", 120, 2, 192)],
        max_batches=1,
        batch_size=2,
        max_total_candles=2,
        max_duration_seconds=30,
        max_storage_growth_bytes=1024,
        rate_limit_seconds=0,
    )

    assert result["session"]["status"] == "PAUSED"
    assert result["session"]["stop_reason"] == "STORAGE_PROTECTION"
    assert result["batches"] == 0


def test_persistent_mt5_failure_does_not_advance_cursor(tmp_path, fake_mt5):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    fake_mt5.last_error = lambda: (-1, "Terminal: Call failed")
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: None
    engine = HistoricalDataEngine(client, store, BrokerTimeConfig.with_offset(0), tmp_path)

    result = engine.collect_session(
        [CollectionPlanItem("EURUSD", "H1", "TIER1", 120, 2, 192)],
        max_batches=1,
        batch_size=2,
        max_total_candles=2,
        max_duration_seconds=30,
        max_storage_growth_bytes=1024 * 1024,
        rate_limit_seconds=0,
    )

    assert result["session"]["status"] == "FAILED"
    assert result["session"]["failed_requests"] == 1
    job = store.collection_jobs()[0]
    assert job["status"] == "FAILED"
    assert job["next_start_time"] is None
