from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


TEMP_RETENTION_DAYS = 7
DIAGNOSTIC_LOG_RETENTION_DAYS = 30
ARCHIVE_REVIEW_DAYS = 90


@dataclass(frozen=True)
class StorageFile:
    path: str
    category: str
    size_bytes: int
    created_at: str | None
    modified_at: str
    eligible: bool
    reason: str
    retention_rule: str
    proposed_action: str

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class StorageCategory:
    category: str
    file_count: int
    size_bytes: int
    retention_rule: str
    eligible_count: int
    eligible_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class StorageReport:
    root: str
    scanned_at: datetime
    disk_total_bytes: int
    disk_free_bytes: int
    disk_used_percent: float
    health_status: str
    project_size_bytes: int
    sqlite_size_bytes: int
    historical_market_data_size_bytes: int
    research_archive_size_bytes: int
    temporary_size_bytes: int
    diagnostic_size_bytes: int
    unclassified_size_bytes: int
    categories: tuple[StorageCategory, ...]
    candidates: tuple[StorageFile, ...]
    estimated_recoverable_bytes: int
    dry_run: bool
    last_cleanup_review: str | None
    next_scheduled_scan: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "scanned_at": self.scanned_at.isoformat(),
            "disk_total_bytes": self.disk_total_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_used_percent": self.disk_used_percent,
            "health_status": self.health_status,
            "project_size_bytes": self.project_size_bytes,
            "sqlite_size_bytes": self.sqlite_size_bytes,
            "historical_market_data_size_bytes": self.historical_market_data_size_bytes,
            "research_archive_size_bytes": self.research_archive_size_bytes,
            "temporary_size_bytes": self.temporary_size_bytes,
            "diagnostic_size_bytes": self.diagnostic_size_bytes,
            "unclassified_size_bytes": self.unclassified_size_bytes,
            "categories": [category.to_dict() for category in self.categories],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "estimated_recoverable_bytes": self.estimated_recoverable_bytes,
            "dry_run": self.dry_run,
            "last_cleanup_review": self.last_cleanup_review,
            "next_scheduled_scan": self.next_scheduled_scan,
            "warnings": list(self.warnings),
        }


class StorageManager:
    """Classify project files and propose retention actions without deleting anything."""

    def __init__(self, root: Path, audit_path: Path | None = None, dry_run: bool = True) -> None:
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"Storage root does not exist: {self.root}")
        self.audit_path = (audit_path or self.root / "logs" / "storage_manager_audit.jsonl").resolve()
        self._assert_inside(self.audit_path)
        self.dry_run = True if dry_run else True

    def _assert_inside(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"Path is outside the project root: {path}") from exc

    def _age_days(self, modified_at: datetime, now: datetime) -> int:
        return max(0, (now - modified_at).days)

    def _classify(self, path: Path, now: datetime) -> StorageFile:
        self._assert_inside(path)
        relative = path.relative_to(self.root)
        display_path = relative.as_posix()
        if path.is_symlink():
            stat = path.lstat()
            return StorageFile(display_path, "UNCLASSIFIED", stat.st_size, None, datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), False, "Symlink or junction is never followed", "No automatic retention", "REVIEW_MANUALLY")
        stat = path.stat()
        created = datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat()
        modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        modified_text = modified.isoformat()
        age = self._age_days(modified, now)
        parts = {part.lower() for part in relative.parts}
        suffix = path.suffix.lower()
        name = path.name.lower()

        if name in {"market_data.sqlite3", "market_data.sqlite3-wal", "market_data.sqlite3-shm"} or suffix in {".sqlite", ".sqlite3", ".db"}:
            return StorageFile(display_path, "PROTECTED", stat.st_size, created, modified_text, False, "Database or database journal is protected", "Never automatically delete", "PROTECT")
        if ".git" in parts or ".venv" in parts or "src" in parts or "tests" in parts or "docs" in parts:
            return StorageFile(display_path, "PROTECTED", stat.st_size, created, modified_text, False, "Application, environment, source, tests, or documentation", "Never automatically delete", "PROTECT")
        if name in {"pyproject.toml", "readme.md", ".env.example", "run_dashboard.ps1", "run_dashboard.cmd"} or suffix == ".lnk":
            return StorageFile(display_path, "PROTECTED", stat.st_size, created, modified_text, False, "Essential application or launcher file", "Never automatically delete", "PROTECT")
        if "logs" in parts:
            eligible = age >= DIAGNOSTIC_LOG_RETENTION_DAYS and suffix in {".log", ".jsonl", ".txt"} and name != "storage_manager_audit.jsonl"
            return StorageFile(display_path, "DIAGNOSTIC", stat.st_size, created, modified_text, eligible, "Debug log exceeds the 30-day review period" if eligible else "Active or recent diagnostic log", "30 days; review before any future purge", "REVIEW_ONLY" if eligible else "KEEP")
        if "diagnostics" in parts:
            return StorageFile(display_path, "DIAGNOSTIC", stat.st_size, created, modified_text, False, "Diagnostic artifact retained for investigation; no automatic purge", "Manual review only", "REVIEW_MANUALLY")
        if parts.intersection({"tmp", "temp", "cache", ".pytest_cache", "__pycache__"}) or suffix in {".tmp", ".temp", ".cache"}:
            eligible = age >= TEMP_RETENTION_DAYS
            return StorageFile(display_path, "TEMPORARY", stat.st_size, created, modified_text, eligible, "Temporary file exceeds the 7-day retention period" if eligible else "Temporary file is within the 7-day retention period", "7 days; dry-run only", "REVIEW_ONLY" if eligible else "KEEP")
        if parts.intersection({"archive", "archives", "research"}):
            eligible = age >= ARCHIVE_REVIEW_DAYS
            return StorageFile(display_path, "ARCHIVE", stat.st_size, created, modified_text, eligible, "Research/archive file reached the 90-day review age" if eligible else "Research/archive file is within the 90-day review period", "90 days; review only, never automatic deletion", "REVIEW_ONLY" if eligible else "KEEP")
        return StorageFile(display_path, "UNCLASSIFIED", stat.st_size, created, modified_text, False, "No safe automatic classification", "No automatic retention", "REVIEW_MANUALLY")

    def _files(self) -> Iterable[Path]:
        for path in self.root.rglob("*"):
            try:
                if path.is_file() or path.is_symlink():
                    yield path
            except OSError:
                continue

    def _audit(self, report: StorageReport) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": "storage_scan",
            "scanned_at": report.scanned_at.isoformat(),
            "dry_run": report.dry_run,
            "health_status": report.health_status,
            "estimated_recoverable_bytes": report.estimated_recoverable_bytes,
            "candidate_count": len(report.candidates),
        }
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def scan(self, now: datetime | None = None) -> StorageReport:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        disk = shutil.disk_usage(self.root)
        used_percent = ((disk.total - disk.free) / disk.total * 100) if disk.total else 0.0
        if used_percent >= 90:
            health = "PROTECTION"
        elif used_percent >= 85:
            health = "HIGH_USAGE"
        elif used_percent >= 70:
            health = "WARNING"
        else:
            health = "HEALTHY"
        records = []
        warnings: list[str] = []
        for path in self._files():
            try:
                records.append(self._classify(path, now))
            except (OSError, ValueError) as exc:
                warnings.append(f"Could not classify {path}: {exc}")
        policies = {
            "PROTECTED": "Never automatically delete",
            "ARCHIVE": "Review after 90 days",
            "TEMPORARY": "Review after 7 days",
            "DIAGNOSTIC": "Logs review after 30 days; artifacts manual review",
            "UNCLASSIFIED": "Manual review only",
        }
        categories = []
        for category, policy in policies.items():
            items = [item for item in records if item.category == category]
            categories.append(StorageCategory(category, len(items), sum(item.size_bytes for item in items), policy, sum(item.eligible for item in items), sum(item.size_bytes for item in items if item.eligible)))
        candidates = tuple(item for item in records if item.eligible)
        sqlite_size = sum(
            item.size_bytes
            for item in records
            if Path(item.path).name.lower() in {
                "market_data.sqlite3",
                "market_data.sqlite3-wal",
                "market_data.sqlite3-shm",
            }
            or Path(item.path).suffix.lower() in {".sqlite", ".sqlite3", ".db"}
        )
        category_sizes = {
            category: sum(item.size_bytes for item in records if item.category == category)
            for category in policies
        }
        report = StorageReport(
            root=str(self.root),
            scanned_at=now,
            disk_total_bytes=disk.total,
            disk_free_bytes=disk.free,
            disk_used_percent=round(used_percent, 2),
            health_status=health,
            project_size_bytes=sum(item.size_bytes for item in records),
            sqlite_size_bytes=sqlite_size,
            historical_market_data_size_bytes=sqlite_size,
            research_archive_size_bytes=category_sizes["ARCHIVE"],
            temporary_size_bytes=category_sizes["TEMPORARY"],
            diagnostic_size_bytes=category_sizes["DIAGNOSTIC"],
            unclassified_size_bytes=category_sizes["UNCLASSIFIED"],
            categories=tuple(categories),
            candidates=candidates,
            estimated_recoverable_bytes=sum(item.size_bytes for item in candidates if item.category == "TEMPORARY"),
            dry_run=self.dry_run,
            last_cleanup_review=None,
            next_scheduled_scan=(now + timedelta(days=1)).isoformat(),
            warnings=tuple(warnings),
        )
        self._audit(report)
        return report

    def cleanup(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Destructive cleanup is disabled; StorageManager is permanently dry-run in Stage 1.6B")
