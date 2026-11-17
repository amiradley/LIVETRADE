from __future__ import annotations

from datetime import datetime, timezone

from live_trade.models import Candle, MarketSnapshot, Tick
from live_trade.storage import MarketDataStore


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
