from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


@dataclass(frozen=True)
class MonitoringHealth:
    state: str
    cycles: int
    thread_alive: bool
    last_started: datetime | None
    last_completed: datetime | None
    last_result: dict[str, Any] | None
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "cycles": self.cycles,
            "thread_alive": self.thread_alive,
            "last_started": self.last_started.isoformat() if self.last_started else None,
            "last_completed": self.last_completed.isoformat() if self.last_completed else None,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }


class DemoMonitoringWorker:
    """Single-instance, restartable worker for supervised DEMO monitoring."""

    def __init__(self, cycle: Callable[[], dict[str, Any]], interval_seconds: float = 60.0) -> None:
        if interval_seconds < 1:
            raise ValueError("Monitoring interval must be at least one second")
        self.cycle = cycle
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = "STOPPED"
        self._cycles = 0
        self._last_started: datetime | None = None
        self._last_completed: datetime | None = None
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None

    def start(self) -> MonitoringHealth:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.health()
            self._stop.clear()
            self._state = "STARTING"
            self._last_error = None
            self._thread = threading.Thread(target=self._run, name="live-trade-demo-monitor", daemon=True)
            self._thread.start()
            return self.health()

    def stop(self, timeout_seconds: float = 10.0) -> MonitoringHealth:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout_seconds)
        with self._lock:
            self._state = "STOPPED" if not thread or not thread.is_alive() else "STOPPING"
            return self.health()

    def _run(self) -> None:
        with self._lock:
            self._state = "RUNNING"
        while not self._stop.is_set():
            started = datetime.now(timezone.utc)
            with self._lock:
                self._last_started = started
            try:
                result = self.cycle()
                with self._lock:
                    self._last_result = result
                    self._last_error = None
                    self._cycles += 1
                    self._last_completed = datetime.now(timezone.utc)
            except Exception as exc:  # keep the worker alive for transient MT5 failures
                with self._lock:
                    self._last_error = str(exc)
                    self._last_completed = datetime.now(timezone.utc)
            self._stop.wait(self.interval_seconds)
        with self._lock:
            self._state = "STOPPED"

    def health(self) -> MonitoringHealth:
        with self._lock:
            return MonitoringHealth(
                state=self._state,
                cycles=self._cycles,
                thread_alive=bool(self._thread and self._thread.is_alive()),
                last_started=self._last_started,
                last_completed=self._last_completed,
                last_result=self._last_result,
                last_error=self._last_error,
            )
