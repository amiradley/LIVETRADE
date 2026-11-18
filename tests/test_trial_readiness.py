from live_trade.storage import MarketDataStore
from live_trade.trial_readiness import build_trial_readiness


def test_trial_readiness_is_not_ready_without_strategy_or_execution(tmp_path):
    report = build_trial_readiness(MarketDataStore(tmp_path / "readiness.sqlite3"))

    assert report["overall_status"] == "NOT_READY"
    assert report["lifecycle_stage"] == "RESEARCH IDEA"
    assert report["execution_enabled"] is False
    statuses = {gate["name"]: gate["status"] for gate in report["gates"]}
    assert statuses["strategy"] == "BLOCKED"
    assert statuses["execution"] == "BLOCKED"
    assert statuses["authorization"] == "BLOCKED"
    assert report["trial_spec"]["authorization_required"] is True


def test_trial_readiness_reports_all_pilot_datasets(tmp_path):
    report = build_trial_readiness(MarketDataStore(tmp_path / "readiness.sqlite3"))
    data_gate = next(gate for gate in report["gates"] if gate["name"] == "data")

    assert len(data_gate["evidence"]["datasets"]) == 8
    assert {row["symbol"] for row in data_gate["evidence"]["datasets"]} == {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"}
