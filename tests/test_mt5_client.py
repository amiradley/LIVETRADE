from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from live_trade.mt5_client import ReadOnlyMt5Client


def test_read_only_client_reads_terminal_symbols_ticks_and_candles(fake_mt5):
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    assert fake_mt5.initialized is True
    assert fake_mt5.initialize_kwargs["timeout"] == 10_000
    assert client.terminal_info()["name"] == "Fake MT5"
    assert len(client.discover_instruments()) == 2
    tick = client.get_tick("EURUSD")
    candles = client.get_candles("EURUSD", "M1", 1)

    assert tick.bid == 1.1
    assert tick.ask == 1.1002
    assert candles[0].close == 1.1005
    assert candles[0].time.tzinfo is not None

    client.disconnect()
    assert fake_mt5.shutdown_called is True


def test_snapshot_reads_all_requested_data(fake_mt5):
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    snapshot = client.snapshot(("EURUSD",), "M5", 1)

    assert len(snapshot.ticks) == 1
    assert len(snapshot.candles) == 1
    assert snapshot.candles[0].timeframe == "M5"


def test_invalid_timeframe_is_rejected(fake_mt5):
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    try:
        client.get_candles("EURUSD", "W1", 1)
    except ValueError as exc:
        assert "Unsupported timeframe" in str(exc)
    else:
        raise AssertionError("Invalid timeframe was accepted")


def test_numpy_structured_candles_are_parsed(fake_mt5):
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: np.array(
        [
            (
                int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()),
                1.1,
                1.101,
                1.099,
                1.1005,
                42,
                2,
                0,
            )
        ],
        dtype=[
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "u8"),
            ("spread", "i4"),
            ("real_volume", "u8"),
        ],
    )
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    candles = client.get_candles("EURUSD", "M1", 1)

    assert len(candles) == 1
    assert candles[0].close == 1.1005
    assert candles[0].tick_volume == 42
    assert candles[0].spread == 2


def test_time_range_candles_preserve_raw_epoch_label(fake_mt5):
    fake_mt5.copy_rates_range = lambda symbol, timeframe, date_from, date_to: [{
        "time": int(datetime(2026, 1, 1, 2, tzinfo=timezone.utc).timestamp()),
        "open": 1.1, "high": 1.101, "low": 1.099, "close": 1.1005,
        "tick_volume": 42, "spread": 2, "real_volume": 0,
    }]
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    candles = client.get_candles_range(
        "EURUSD", "H1", datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
    )

    assert candles[0].time == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)


def test_invalid_numpy_candle_value_is_rejected(fake_mt5):
    fake_mt5.copy_rates_from_pos = lambda symbol, timeframe, start, count: np.array(
        [(1798761600, np.nan, 1.101, 1.099, 1.1005, 42, 2, 0)],
        dtype=[
            ("time", "i8"),
            ("open", "f8"),
            ("high", "f8"),
            ("low", "f8"),
            ("close", "f8"),
            ("tick_volume", "u8"),
            ("spread", "i4"),
            ("real_volume", "u8"),
        ],
    )
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    try:
        client.get_candles("EURUSD", "M1", 1)
    except Exception as exc:
        assert "non-finite numeric field open" in str(exc)
    else:
        raise AssertionError("Invalid candle value was accepted")
