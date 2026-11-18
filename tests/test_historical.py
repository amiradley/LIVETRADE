from datetime import datetime, timezone
from threading import RLock

from live_trade.historical import CollectionPlanItem, HistoricalDataEngine, validate_candles
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
    plan = [CollectionPlanItem("EURUSD", "M1", "TIER1", 120, 2, 192)]
    result = engine.collect(plan, max_batches=1, batch_size=1, rate_limit_seconds=0)
    second = engine.collect(plan, max_batches=1, batch_size=1, rate_limit_seconds=0)

    assert summary["total"] == 1
    assert reports[0].candle_count == 1
    assert result["batches"] == 1
    assert second["historical"]["candles"] == 2
    assert store.historical_summary()["candles"] == 2
    assert store.coverage_summary()["by_timeframe"]["M1"]["audited"] == 1
    assert store.collection_jobs()[0]["downloaded_bars"] == 2


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
