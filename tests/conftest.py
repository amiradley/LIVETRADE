from __future__ import annotations

from datetime import datetime, timezone

import pytest


class FakeMt5:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440

    def __init__(self) -> None:
        self.initialized = False
        self.shutdown_called = False
        self.tick_epoch = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())

    def initialize(self, **kwargs):
        self.initialized = True
        self.initialize_kwargs = kwargs
        return True

    def shutdown(self):
        self.shutdown_called = True

    def last_error(self):
        return (0, "ok")

    def terminal_info(self):
        return {"name": "Fake MT5", "connected": True}

    def account_info(self):
        return {
            "login": 12345,
            "trade_mode": 0,
            "currency": "USD",
            "balance": 10000.0,
            "equity": 10000.0,
            "margin": 0.0,
            "margin_free": 10000.0,
        }

    def positions_get(self):
        return ()

    def symbols_get(self):
        return (
            {"name": "EURUSD", "visible": True, "point": 0.00001, "digits": 5, "trade_mode": 4},
            {"name": "GBPUSD", "visible": True, "point": 0.00001, "digits": 5, "trade_mode": 4},
        )

    def symbol_info_tick(self, symbol):
        return {
            "time": self.tick_epoch,
            "bid": 1.1000,
            "ask": 1.1002,
            "last": 1.1001,
            "volume": 10,
        }

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        return [
            {
                "time": int(datetime(2026, 1, 1, 0, tzinfo=timezone.utc).timestamp()),
                "open": 1.1,
                "high": 1.101,
                "low": 1.099,
                "close": 1.1005,
                "tick_volume": 42,
                "spread": 2,
                "real_volume": 0,
            }
        ]


@pytest.fixture
def fake_mt5():
    return FakeMt5()
