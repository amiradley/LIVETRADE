from live_trade.finance import FinancialDataService
from live_trade.mt5_client import ReadOnlyMt5Client
from live_trade.session import SessionMode, SessionState, TradingSessionController
from live_trade.storage import MarketDataStore


def test_research_session_start_and_safe_stop(tmp_path, fake_mt5):
    client = ReadOnlyMt5Client(api=fake_mt5)
    store = MarketDataStore(tmp_path / "session.sqlite3")
    controller = TradingSessionController(
        client,
        FinancialDataService(client),
        store,
        mode=SessionMode.RESEARCH,
    )

    started = controller.start()
    stopped = controller.stop()

    assert started.state is SessionState.RUNNING
    assert started.session_id is not None
    assert stopped.state is SessionState.STOPPED
    assert fake_mt5.shutdown_called is True


def test_session_does_not_claim_stop_when_positions_remain(tmp_path, fake_mt5):
    fake_mt5.positions_get = lambda: (
        {
            "ticket": 7,
            "symbol": "EURUSD",
            "type": 0,
            "volume": 0.01,
            "price_open": 1.1,
            "price_current": 1.099,
            "profit": -1.0,
            "time": 1767225600,
        },
    )
    client = ReadOnlyMt5Client(api=fake_mt5)
    controller = TradingSessionController(
        client,
        FinancialDataService(client),
        MarketDataStore(tmp_path / "session.sqlite3"),
        mode=SessionMode.DEMO_SHADOW,
    )

    controller.start()
    stopped = controller.stop()

    assert stopped.state is SessionState.UNRESOLVED_POSITIONS
    assert len(stopped.financial.positions) == 1
    assert fake_mt5.shutdown_called is False
