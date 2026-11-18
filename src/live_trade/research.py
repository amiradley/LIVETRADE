from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .scanner import MarketScanner, ScannerConfig
from .storage import MarketDataStore
from .technical import TechnicalAnalyzer, analyze_multi_timeframe
from .timestamping import BrokerTimeConfig


@dataclass(frozen=True)
class ResearchWorkerHealth:
    state: str
    cycles: int
    thread_alive: bool
    last_started: datetime | None
    last_completed: datetime | None
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "cycles": self.cycles,
            "thread_alive": self.thread_alive,
            "last_started": self.last_started.isoformat() if self.last_started else None,
            "last_completed": self.last_completed.isoformat() if self.last_completed else None,
            "last_error": self.last_error,
        }


class ResearchWorker:
    """Read-only scanner, technical-analysis, and market-monitoring worker."""

    def __init__(
        self,
        client: Any,
        store: MarketDataStore,
        symbol: str,
        client_lock: threading.RLock,
        interval_seconds: float = 60.0,
        scanner_config: ScannerConfig | None = None,
        broker_time: BrokerTimeConfig | None = None,
    ) -> None:
        if interval_seconds < 1:
            raise ValueError("Research interval must be at least one second")
        self.client = client
        self.store = store
        self.symbol = symbol.upper()
        self.client_lock = client_lock
        self.interval_seconds = interval_seconds
        self.scanner_config = scanner_config or ScannerConfig(
            timeframes=("M1", "M5"), bars=60, max_active_instruments=1, broker_time=broker_time
        )
        self.analyzer = TechnicalAnalyzer()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "STOPPED"
        self._cycles = 0
        self._last_started: datetime | None = None
        self._last_completed: datetime | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Research worker is already running")
        self._stop.clear()
        self._state = "STARTING"
        self._last_error = None
        self._thread = threading.Thread(target=self._run, name="live-trade-research", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self._state = "STOPPED" if not self._thread or not self._thread.is_alive() else "STOPPING"

    def _run(self) -> None:
        self._state = "RUNNING"
        while not self._stop.is_set():
            self._run_cycle()
            self._stop.wait(self.interval_seconds)
        self._state = "STOPPED"

    def _run_cycle(self) -> None:
        self._last_started = datetime.now(timezone.utc)
        try:
            with self.client_lock:
                scan = MarketScanner(self.client, self.scanner_config).scan((self.symbol,))
                self.store.save_scan(scan)
                candles_by_timeframe = {
                    timeframe: self.client.get_candles(self.symbol, timeframe, self.scanner_config.bars)
                    for timeframe in self.scanner_config.timeframes
                }
                analyses = {
                    timeframe: self.analyzer.analyze(self.symbol, timeframe, candles, closed_only=True)
                    for timeframe, candles in candles_by_timeframe.items()
                }
                for analysis in analyses.values():
                    self.store.save_technical_analysis(analysis)
                self.store.save_multi_timeframe_analysis(
                    analyze_multi_timeframe(self.symbol, candles_by_timeframe, self.analyzer, closed_only=True)
                )
            self._cycles += 1
            self._last_completed = datetime.now(timezone.utc)
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)

    def health(self) -> ResearchWorkerHealth:
        return ResearchWorkerHealth(
            state=self._state,
            cycles=self._cycles,
            thread_alive=bool(self._thread and self._thread.is_alive()),
            last_started=self._last_started,
            last_completed=self._last_completed,
            last_error=self._last_error,
        )
