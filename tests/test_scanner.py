from datetime import datetime, timezone

import pytest

from live_trade.mt5_client import ReadOnlyMt5Client
from live_trade.scanner import MarketScanner, ScannerConfig, market_session


def test_scanner_ranks_spread_and_caps_activation(fake_mt5):
    captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    scanner = MarketScanner(
        client,
        ScannerConfig(timeframes=("M1", "M5"), bars=1, max_active_instruments=1),
    )

    scan = scanner.scan(captured_at=captured_at)

    assert scan.discovered_count == 2
    assert len(scan.results) == 2
    assert all(result.eligible for result in scan.results)
    assert sum(result.activated for result in scan.results) == 1
    assert scan.results[0].spread_points == pytest.approx(20.0)
    assert scan.results[0].candle_counts == {"M1": 1, "M5": 1}


def test_scanner_rejects_stale_ticks(fake_mt5):
    captured_at = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    scanner = MarketScanner(
        client,
        ScannerConfig(timeframes=("M1",), bars=1, max_tick_age_seconds=30),
    )

    scan = scanner.scan(captured_at=captured_at)

    assert all(not result.eligible for result in scan.results)
    assert all("stale_tick" in result.reasons for result in scan.results)


def test_market_session_uses_utc_and_marks_weekends():
    assert market_session(datetime(2026, 1, 1, 8, tzinfo=timezone.utc)) == "EUROPE"
    assert market_session(datetime(2026, 1, 3, 8, tzinfo=timezone.utc)) == "CLOSED_WEEKEND"


def test_future_tick_requires_explicit_verified_server_offset(fake_mt5):
    fake_mt5.tick_epoch = int(datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp())
    captured_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    rejected = MarketScanner(
        client,
        ScannerConfig(timeframes=("M1",), bars=1, max_tick_age_seconds=30),
    ).scan(captured_at=captured_at)
    accepted = MarketScanner(
        client,
        ScannerConfig(
            timeframes=("M1",),
            bars=1,
            max_tick_age_seconds=30,
            broker_server_utc_offset_seconds=7200,
        ),
    ).scan(captured_at=captured_at)

    rejected_result = rejected.results[0]
    accepted_result = accepted.results[0]
    assert rejected_result.eligible is False
    assert "future_tick" in rejected_result.reasons
    assert accepted_result.eligible is True
    assert accepted_result.timestamp_offset_seconds == 7200
    assert accepted_result.tick_time == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    assert accepted_result.effective_tick_time == captured_at
