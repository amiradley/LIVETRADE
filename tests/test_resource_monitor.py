from pathlib import Path

from live_trade.resource_monitor import ResourceMonitor, protection_state


def test_resource_protection_thresholds_are_explicit():
    assert protection_state(50, 50) == "HEALTHY"
    assert protection_state(75, 50) == "WARNING"
    assert protection_state(50, 85) == "PROTECTION"
    assert protection_state(90, 50) == "CRITICAL"
    assert protection_state(None, None) == "HEALTHY"


def test_resource_snapshot_reports_unavailable_values_as_none_when_not_supported(tmp_path):
    snapshot = ResourceMonitor(Path(tmp_path), Path(tmp_path / "missing.sqlite3")).capture()

    assert snapshot.disk_used_percent is not None
    assert snapshot.sqlite_bytes == 0
    assert snapshot.protection_state in {"HEALTHY", "WARNING", "PROTECTION", "CRITICAL"}
