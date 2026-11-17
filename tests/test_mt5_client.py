from __future__ import annotations

from live_trade.mt5_client import ReadOnlyMt5Client


def test_read_only_client_reads_terminal_symbols_ticks_and_candles(fake_mt5):
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    assert fake_mt5.initialized is True
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
