import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from live_trade.storage_manager import StorageManager


def aged(path: Path, days: int, now: datetime) -> None:
    timestamp = (now - timedelta(days=days)).timestamp()
    path.touch()
    os.utime(path, (timestamp, timestamp))


def by_name(report, name: str):
    return next(item for item in report.candidates if item.path == name)


def test_storage_classifies_retention_candidates_and_protects_database(tmp_path):
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    old_temp = tmp_path / "temp" / "old.tmp"
    new_temp = tmp_path / "temp" / "new.tmp"
    old_log = tmp_path / "logs" / "old.log"
    new_log = tmp_path / "logs" / "new.log"
    old_archive = tmp_path / "research" / "archive" / "old.json"
    database = tmp_path / "data" / "market_data.sqlite3"
    source = tmp_path / "src" / "module.py"
    for path in (old_temp, new_temp, old_log, new_log, old_archive, database, source):
        path.parent.mkdir(parents=True, exist_ok=True)
    aged(old_temp, 8, now)
    aged(new_temp, 2, now)
    aged(old_log, 31, now)
    aged(new_log, 2, now)
    aged(old_archive, 91, now)
    database.write_bytes(b"sqlite")
    source.write_bytes(b"source")

    report = StorageManager(tmp_path).scan(now=now)

    assert report.dry_run is True
    assert report.project_size_bytes >= 6 + 6
    assert report.sqlite_size_bytes == 6
    assert report.historical_market_data_size_bytes == 6
    assert report.research_archive_size_bytes == old_archive.stat().st_size
    assert report.temporary_size_bytes == old_temp.stat().st_size + new_temp.stat().st_size
    assert report.diagnostic_size_bytes == old_log.stat().st_size + new_log.stat().st_size
    assert by_name(report, "temp/old.tmp").category == "TEMPORARY"
    assert by_name(report, "logs/old.log").category == "DIAGNOSTIC"
    assert by_name(report, "research/archive/old.json").category == "ARCHIVE"
    assert all(item.path not in {"data/market_data.sqlite3", "src/module.py"} for item in report.candidates)
    assert report.estimated_recoverable_bytes == old_temp.stat().st_size


def test_storage_dry_run_never_deletes_and_writes_audit(tmp_path):
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    candidate = tmp_path / "temp" / "old.tmp"
    candidate.parent.mkdir()
    aged(candidate, 10, now)
    manager = StorageManager(tmp_path)
    before = candidate.read_bytes()

    report = manager.scan(now=now)

    with pytest.raises(RuntimeError, match="Destructive cleanup is disabled"):
        manager.cleanup()
    assert candidate.exists()
    assert candidate.read_bytes() == before
    audit = tmp_path / "logs" / "storage_manager_audit.jsonl"
    assert audit.exists()
    assert '"event": "storage_scan"' in audit.read_text(encoding="utf-8")
    assert report.candidates


def test_storage_rejects_paths_outside_root_and_disk_threshold(monkeypatch, tmp_path):
    manager = StorageManager(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValueError, match="outside the project root"):
        manager._assert_inside(outside)

    class Usage:
        total = 100
        used = 90
        free = 10

    monkeypatch.setattr("live_trade.storage_manager.shutil.disk_usage", lambda _: Usage())
    assert manager.scan().health_status == "PROTECTION"
