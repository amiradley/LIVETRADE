from datetime import date, datetime, timezone

from live_trade.mt5_client import ReadOnlyMt5Client
from live_trade.research import ResearchWorker
from live_trade.scanner import MarketScanner, ScannerConfig
from live_trade.storage import MarketDataStore
from live_trade.timestamping import BrokerTimeConfig, TimestampConfigurationError


def test_metaquotes_demo_configuration_is_explicit_and_audited():
    config = BrokerTimeConfig.metaquotes_demo_default()

    assert config.server_name == "MetaQuotes-Demo"
    assert config.utc_offset_hours == 2.0
    assert config.utc_offset_seconds == 7200
    assert config.verified_at == date(2026, 11, 17)
    assert "Verified" in config.source
    assert config.effective_utc(datetime(2026, 1, 1, 2, tzinfo=timezone.utc)) == datetime(
        2026, 1, 1, tzinfo=timezone.utc
    )


def test_unknown_broker_without_offset_fails_closed(monkeypatch):
    monkeypatch.setenv("LIVE_TRADE_BROKER_SERVER", "Unknown-Demo")
    monkeypatch.delenv("LIVE_TRADE_BROKER_UTC_OFFSET_HOURS", raising=False)

    config = BrokerTimeConfig.from_environment()

    assert config.configured is False
    try:
        config.effective_utc(datetime(2026, 1, 1, tzinfo=timezone.utc))
    except TimestampConfigurationError:
        pass
    else:
        raise AssertionError("unconfigured broker time must fail closed")


def test_scanner_uses_central_config_and_preserves_raw_tick_time(fake_mt5):
    fake_mt5.tick_epoch = int(datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp())
    broker_time = BrokerTimeConfig.metaquotes_demo_default()
    client = ReadOnlyMt5Client(api=fake_mt5, broker_time=broker_time)
    client.connect()

    result = MarketScanner(
        client,
        ScannerConfig(timeframes=("M1",), bars=1, max_tick_age_seconds=30, broker_time=broker_time),
    ).scan(captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc)).results[0]

    assert result.eligible is True
    assert result.tick_time == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    assert result.effective_tick_time == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert result.timestamp_offset_seconds == 7200
    assert result.timestamp_configured is True


def test_scanner_rejects_missing_configuration_and_real_future_tick(fake_mt5):
    fake_mt5.tick_epoch = int(datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp())
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    captured = datetime(2026, 1, 1, tzinfo=timezone.utc)

    missing = MarketScanner(
        client,
        ScannerConfig(
            timeframes=("M1",),
            bars=1,
            max_tick_age_seconds=30,
            broker_time=BrokerTimeConfig("Unknown", None, "missing", None),
        ),
    ).scan(captured_at=captured).results[0]
    future = MarketScanner(
        client,
        ScannerConfig(
            timeframes=("M1",),
            bars=1,
            max_tick_age_seconds=30,
            broker_time=BrokerTimeConfig.with_offset(0),
        ),
    ).scan(captured_at=captured).results[0]

    assert missing.eligible is False
    assert "timestamp_configuration_missing" in missing.reasons
    assert future.eligible is False
    assert "future_tick" in future.reasons


def test_worker_receives_the_same_central_timestamp_configuration(fake_mt5, tmp_path):
    broker_time = BrokerTimeConfig.metaquotes_demo_default()
    worker = ResearchWorker(
        ReadOnlyMt5Client(api=fake_mt5),
        MarketDataStore(tmp_path / "market.sqlite3"),
        "EURUSD",
        __import__("threading").RLock(),
        broker_time=broker_time,
    )

    assert worker.scanner_config.broker_time == broker_time
    assert worker.scanner_config.timestamp_configured is True
    assert worker.scanner_config.broker_server_utc_offset_seconds == 7200


def test_timestamp_configuration_is_persisted_with_scan(fake_mt5, tmp_path):
    broker_time = BrokerTimeConfig.metaquotes_demo_default()
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    scan = MarketScanner(
        client,
        ScannerConfig(timeframes=("M1",), bars=1, broker_time=broker_time),
    ).scan(captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    store = MarketDataStore(tmp_path / "market.sqlite3")
    scan_id = store.save_scan(scan)

    with store.connect() as connection:
        row = connection.execute(
            "SELECT timestamp_offset_seconds, timestamp_configured, timestamp_config_source "
            "FROM scan_results WHERE scan_id = ?",
            (scan_id,),
        ).fetchone()
    assert row[0] == 7200
    assert row[1] == 1
    assert "Verified" in row[2]
