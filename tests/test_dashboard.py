import threading
from datetime import date, datetime, timezone

from live_trade.config import Settings
from live_trade.dashboard import DashboardServer, DashboardService
from live_trade.intelligence import ExchangeRate
from live_trade.session import SessionError, SessionState
from live_trade.timestamping import BrokerTimeConfig


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
    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))

    overview = service.overview()

    assert overview["connection"]["status"] == "CONNECTED"
    assert overview["financial"]["balance"]["status"] == "BROKER_VERIFIED"
    assert overview["financial"]["balance"]["value"] == 10000.0
    assert overview["financial"]["floating_pnl"]["status"] == "INTERNALLY_CALCULATED"
    assert overview["financial"]["currency_conversion"]["status"] == "UNAVAILABLE"
    assert overview["financial"]["position_count"] == 0
    assert overview["broker_time"]["utc_offset_hours"] == 2.0


def test_dashboard_storage_report_is_dry_run(tmp_path, fake_mt5):
    from live_trade.mt5_client import ReadOnlyMt5Client

    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))
    report = service.storage()

    assert report["dry_run"] is True
    assert report["project_size_bytes"] >= 0
    assert report["sqlite_size_bytes"] >= 0


def test_dashboard_exposes_historical_data_status(tmp_path, fake_mt5):
    from live_trade.mt5_client import ReadOnlyMt5Client

    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))
    report = service.historical()

    assert report["instruments"]["total"] == 0
    assert report["collection"]["active_scheduler"] is False
    assert report["historical"]["candles"] == 0


def test_dashboard_exposes_trial_readiness_without_enabling_execution(tmp_path, fake_mt5):
    from live_trade.mt5_client import ReadOnlyMt5Client

    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))
    report = service.trial_readiness()

    assert report["overall_status"] == "NOT_READY"
    assert report["execution_enabled"] is False
    assert any(gate["name"] == "authorization" and gate["status"] == "BLOCKED" for gate in report["gates"])


def test_dashboard_start_stop_and_duplicate_prevention(tmp_path, fake_mt5, monkeypatch):
    from live_trade.mt5_client import ReadOnlyMt5Client

    monkeypatch.setenv("LIVE_TRADE_FX_URL", "http://127.0.0.1:1/unavailable")
    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))

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


def test_dashboard_browser_refresh_preserves_session(tmp_path, fake_mt5, monkeypatch):
    from live_trade.mt5_client import ReadOnlyMt5Client

    monkeypatch.setenv("LIVE_TRADE_FX_URL", "http://127.0.0.1:1/unavailable")
    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))
    service.research_worker = None
    service.controller.research_worker = None

    started = service.start()
    first = service.overview()
    second = service.overview()

    assert started["session"]["state"] == SessionState.RUNNING.value
    assert first["session"]["state"] == SessionState.RUNNING.value
    assert second["session"]["state"] == SessionState.RUNNING.value
    assert first["session"]["session_id"] == second["session"]["session_id"]
    service.stop()


def test_dashboard_marks_running_session_error_after_mt5_failure(tmp_path, fake_mt5, monkeypatch):
    from live_trade.mt5_client import ReadOnlyMt5Client

    monkeypatch.setenv("LIVE_TRADE_FX_URL", "http://127.0.0.1:1/unavailable")
    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))
    service.research_worker = None
    service.controller.research_worker = None
    service.start()
    fake_mt5.terminal_info = lambda: (_ for _ in ()).throw(RuntimeError("simulated MT5 disconnect"))

    stale = service.overview()

    assert stale["connection"]["status"] == "STALE"
    assert stale["session"]["state"] == SessionState.ERROR.value
    assert stale["session"]["message"] == "simulated MT5 disconnect"


def test_dashboard_monitoring_lifecycle_is_single_instance_and_entries_stay_blocked(tmp_path, fake_mt5):
    from live_trade.mt5_client import ReadOnlyMt5Client

    service = DashboardService(dashboard_settings(tmp_path), client=ReadOnlyMt5Client(api=fake_mt5))
    completed = threading.Event()
    service.demo_monitor.cycle = lambda: (completed.set() or {"status": "READ_ONLY"})

    gate = service.enable_demo_entries()
    assert gate["status"] == "BLOCKED"
    assert gate["risk_gate"]["reasons"] == ["risk_configuration_or_exchange_rate_unavailable"]

    started = service.start_monitoring()
    assert started["status"] == "MONITORING"
    assert started["execution_enabled"] is False
    assert completed.wait(2)

    duplicate = service.start_monitoring()
    assert duplicate["monitoring"]["thread_alive"] is True
    stopped = service.stop_monitoring()
    assert stopped["status"] == "STOPPED"
    assert stopped["monitoring"]["thread_alive"] is False
    assert stopped["reconciliation"]["status"] == "RECONCILED"


def test_dashboard_entry_activation_requires_live_preflight_and_can_be_stopped(tmp_path, fake_mt5):
    from live_trade.mt5_client import ReadOnlyMt5Client
    from test_execution import ExecutionApi

    settings = Settings(
        base_dir=tmp_path,
        mt5_path=None,
        db_path=tmp_path / "dashboard.sqlite3",
        log_path=tmp_path / "dashboard.jsonl",
        symbols=("EURUSD",),
        broker_time=BrokerTimeConfig.with_offset(0),
    )
    api = ExecutionApi()
    service = DashboardService(settings, client=ReadOnlyMt5Client(api=api))
    service.store.save_exchange_rate(
        ExchangeRate("USD", "MYR", 4.0, "test-recorded-rate", datetime.now(timezone.utc), date.today(), "OK")
    )

    enabled = service.enable_demo_entries()
    assert enabled["status"] == "ENABLED"
    assert enabled["execution_enabled"] is True
    assert enabled["risk_gate"]["entries_allowed"] is True
    assert service.demo_entries_enabled is True

    stopped = service.disable_demo_entries()
    assert stopped["status"] == "STOPPED"
    assert service.demo_entries_enabled is False
    assert api.order_send_calls == 0
