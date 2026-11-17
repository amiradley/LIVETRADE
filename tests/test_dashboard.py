from live_trade.config import Settings
from live_trade.dashboard import DashboardServer, DashboardService
from live_trade.session import SessionError, SessionState


def dashboard_settings(tmp_path):
    return Settings(
        base_dir=tmp_path,
        mt5_path=None,
        db_path=tmp_path / "dashboard.sqlite3",
        log_path=tmp_path / "dashboard.jsonl",
        symbols=("EURUSD",),
    )


def test_dashboard_exposes_verified_financial_fields(tmp_path, fake_mt5, monkeypatch):
    from live_trade.mt5_client import ReadOnlyMt5Client

    monkeypatch.setenv("LIVE_TRADE_FX_URL", "http://127.0.0.1:1/unavailable")
    service = DashboardService(dashboard_settings(tmp_path))
    service.client = ReadOnlyMt5Client(api=fake_mt5)
    service.financial.client = service.client
    service.controller.client = service.client
    service.controller.financial_service = service.financial

    overview = service.overview()

    assert overview["connection"]["status"] == "CONNECTED"
    assert overview["financial"]["balance"]["status"] == "BROKER_VERIFIED"
    assert overview["financial"]["balance"]["value"] == 10000.0
    assert overview["financial"]["floating_pnl"]["status"] == "INTERNALLY_CALCULATED"
    assert overview["financial"]["currency_conversion"]["status"] == "UNAVAILABLE"
    assert overview["financial"]["position_count"] == 0


def test_dashboard_start_stop_and_duplicate_prevention(tmp_path, fake_mt5, monkeypatch):
    from live_trade.mt5_client import ReadOnlyMt5Client

    monkeypatch.setenv("LIVE_TRADE_FX_URL", "http://127.0.0.1:1/unavailable")
    service = DashboardService(dashboard_settings(tmp_path))
    service.client = ReadOnlyMt5Client(api=fake_mt5)
    service.financial.client = service.client
    service.controller.client = service.client
    service.controller.financial_service = service.financial

    started = service.start()
    assert started["session"]["state"] == SessionState.RUNNING.value

    try:
        service.start()
    except SessionError:
        pass
    else:
        raise AssertionError("duplicate START must be rejected")

    stopped = service.stop()
    assert stopped["session"]["state"] == SessionState.STOPPED.value
    assert fake_mt5.shutdown_called is True


def test_dashboard_server_rejects_duplicate_bind(tmp_path, fake_mt5):
    service = DashboardService(dashboard_settings(tmp_path))
    first = DashboardServer(("127.0.0.1", 0), service)
    try:
        address = first.server_address
        try:
            DashboardServer(address, service)
        except OSError:
            pass
        else:
            raise AssertionError("duplicate dashboard bind must be rejected")
    finally:
        first.server_close()
