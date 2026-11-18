from __future__ import annotations

import ctypes
import os
import shutil
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


RAM_WARNING_PERCENT = 75.0
RAM_PROTECTION_PERCENT = 85.0
RAM_CRITICAL_PERCENT = 90.0
DISK_WARNING_PERCENT = 70.0
DISK_PROTECTION_PERCENT = 85.0
DISK_CRITICAL_PERCENT = 90.0


@dataclass(frozen=True)
class ResourceSnapshot:
    captured_at: str
    ram_total_mb: float | None
    ram_available_mb: float | None
    ram_used_percent: float | None
    process_memory_mb: float | None
    cpu_percent: float | None
    disk_used_percent: float | None
    sqlite_bytes: int | None
    telemetry_source: str
    protection_state: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def protection_state(ram_used_percent: float | None, disk_used_percent: float | None) -> str:
    values = [value for value in (ram_used_percent, disk_used_percent) if value is not None]
    if any(value >= RAM_CRITICAL_PERCENT or value >= DISK_CRITICAL_PERCENT for value in values):
        return "CRITICAL"
    if any(value >= RAM_PROTECTION_PERCENT or value >= DISK_PROTECTION_PERCENT for value in values):
        return "PROTECTION"
    if any(value >= RAM_WARNING_PERCENT or value >= DISK_WARNING_PERCENT for value in values):
        return "WARNING"
    return "HEALTHY"


class ResourceMonitor:
    """Small read-only Windows resource sampler with safe fallbacks."""

    def __init__(self, project_root: Path, sqlite_path: Path) -> None:
        self.project_root = project_root
        self.sqlite_path = sqlite_path
        self._previous_cpu: tuple[int, int, int, int] | None = None

    def _memory(self) -> tuple[float | None, float | None, float | None, float | None, str]:
        if os.name != "nt":
            return None, None, None, None, "Windows telemetry unavailable on this platform"
        try:
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("memory_load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong), ("available_extended", ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError("GlobalMemoryStatusEx failed")
            total = status.total / (1024 * 1024)
            available = status.available / (1024 * 1024)
            used = 100.0 * (total - available) / total if total else None
            return total, available, used, None, "Windows GlobalMemoryStatusEx; process working set unavailable"
        except (AttributeError, OSError, TypeError):
            return None, None, None, None, "Windows memory telemetry unavailable"

    def _cpu(self) -> float | None:
        if os.name != "nt":
            return None
        try:
            class FileTime(ctypes.Structure):
                _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]
            idle, kernel, user = FileTime(), FileTime(), FileTime()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            values = tuple((item.high << 32) | item.low for item in (idle, kernel, user))
            if self._previous_cpu is None:
                self._previous_cpu = (values[0], values[1], values[2], time.monotonic_ns())
                return None
            prior_idle, prior_kernel, prior_user, _ = self._previous_cpu
            self._previous_cpu = (values[0], values[1], values[2], time.monotonic_ns())
            idle_delta = values[0] - prior_idle
            total_delta = (values[1] - prior_kernel) + (values[2] - prior_user)
            total = idle_delta + total_delta
            return max(0.0, min(100.0, 100.0 * (total - idle_delta) / total)) if total else None
        except (AttributeError, OSError, TypeError):
            return None

    def capture(self) -> ResourceSnapshot:
        total, available, used, process, source = self._memory()
        disk = shutil.disk_usage(self.project_root)
        disk_used = 100.0 * (disk.total - disk.free) / disk.total if disk.total else None
        return ResourceSnapshot(
            captured_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            ram_total_mb=total,
            ram_available_mb=available,
            ram_used_percent=used,
            process_memory_mb=process,
            cpu_percent=self._cpu(),
            disk_used_percent=disk_used,
            sqlite_bytes=self.sqlite_path.stat().st_size if self.sqlite_path.exists() else 0,
            telemetry_source=source,
            protection_state=protection_state(used, disk_used),
        )
