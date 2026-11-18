from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .instrument_intelligence import classify_instrument, priority_for
from .models import Candle
from .mt5_client import Mt5Error
from .resource_monitor import ResourceMonitor, ResourceSnapshot
from .storage import MarketDataStore
from .storage_manager import StorageManager
from .timestamping import BrokerTimeConfig, TimestampConfigurationError


TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}
DEFAULT_BATCH_SIZE = 200
MIN_RESEARCH_BARS = 100
TIMEFRAME_TARGETS = {
    "M1": {"INITIAL": 5_000, "INTERMEDIATE": 25_000, "EXTENDED": 100_000},
    "M5": {"INITIAL": 3_000, "INTERMEDIATE": 15_000, "EXTENDED": 60_000},
    "M15": {"INITIAL": 2_000, "INTERMEDIATE": 10_000, "EXTENDED": 40_000},
    "M30": {"INITIAL": 1_500, "INTERMEDIATE": 7_500, "EXTENDED": 30_000},
    "H1": {"INITIAL": 1_000, "INTERMEDIATE": 5_000, "EXTENDED": 20_000},
    "H4": {"INITIAL": 750, "INTERMEDIATE": 3_000, "EXTENDED": 12_000},
    "D1": {"INITIAL": 500, "INTERMEDIATE": 2_000, "EXTENDED": 8_000},
}
TIMEFRAME_COLLECTION_PRIORITY = ("H1", "M15", "M5", "M30", "H4", "D1", "M1")
TRANSIENT_MT5_ERROR_CODES = {-1, -10004, -10005, -10006, -10007}


def target_for(timeframe: str, level: str = "INITIAL") -> int:
    return int(TIMEFRAME_TARGETS[timeframe.upper()][level.upper()])


def research_readiness(candle_count: int, timeframe: str, data_quality: str, gap_count: int, target_level: str = "INITIAL") -> str:
    if candle_count == 0:
        return "NOT_COLLECTED"
    if data_quality in {"INVALID", "ERROR"}:
        return "INVALID"
    if gap_count:
        return "DEGRADED"
    if candle_count >= target_for(timeframe, "EXTENDED") and target_level.upper() == "EXTENDED":
        return "READY_FOR_EXTENDED_VALIDATION"
    if candle_count >= target_for(timeframe, "INTERMEDIATE") and target_level.upper() in {"INTERMEDIATE", "EXTENDED"}:
        return "READY_FOR_DEVELOPMENT_RESEARCH"
    if candle_count >= target_for(timeframe, "INITIAL"):
        return "READY_FOR_EXPLORATORY_RESEARCH"
    return "INSUFFICIENT_DATA"


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _raw_time(candle: Candle) -> datetime:
    return _utc(candle.time)


def _effective_time(candle: Candle, broker_time: BrokerTimeConfig) -> datetime:
    return broker_time.effective_utc(_raw_time(candle))


def _is_complete(candle: Candle, timeframe: str, broker_time: BrokerTimeConfig, captured_at: datetime) -> bool:
    return _effective_time(candle, broker_time) + timedelta(minutes=TIMEFRAME_MINUTES[timeframe.upper()]) <= _utc(captured_at)


def _error_details(exc: Exception) -> dict[str, Any]:
    details = getattr(exc, "details", None)
    return details if isinstance(details, dict) else {}


@dataclass(frozen=True)
class QualityReport:
    symbol: str
    timeframe: str
    candle_count: int
    complete_candle_count: int
    earliest_candle_time: datetime | None
    latest_candle_time: datetime | None
    gap_count: int
    duplicate_count: int
    invalid_count: int
    timestamp_integrity: str
    latest_candle_complete: bool
    data_quality: str
    readiness: str
    issues: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "candle_count": self.candle_count,
            "complete_candle_count": self.complete_candle_count,
            "earliest_candle_time": self.earliest_candle_time.isoformat() if self.earliest_candle_time else None,
            "latest_candle_time": self.latest_candle_time.isoformat() if self.latest_candle_time else None,
            "gap_count": self.gap_count,
            "duplicate_count": self.duplicate_count,
            "invalid_count": self.invalid_count,
            "timestamp_integrity": self.timestamp_integrity,
            "latest_candle_complete": self.latest_candle_complete,
            "data_quality": self.data_quality,
            "readiness": self.readiness,
            "issues": list(self.issues),
        }


def validate_candles(
    symbol: str,
    timeframe: str,
    candles: Sequence[Candle],
    broker_time: BrokerTimeConfig,
    captured_at: datetime | None = None,
    minimum_history: int = MIN_RESEARCH_BARS,
) -> QualityReport:
    timeframe = timeframe.upper()
    interval = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    now = _utc(captured_at or datetime.now(timezone.utc))
    issues: list[dict[str, Any]] = []
    effective_times: list[datetime] = []
    seen: set[datetime] = set()
    first_by_time: dict[datetime, Candle] = {}
    first_index_by_time: dict[datetime, int] = {}
    duplicate_count = 0
    invalid_count = 0
    timestamp_integrity = "VALID"
    for index, candle in enumerate(candles):
        try:
            effective = broker_time.effective_utc(_utc(candle.time))
        except TimestampConfigurationError:
            effective = _utc(candle.time)
            timestamp_integrity = "UNCONFIGURED"
            issues.append({"index": index, "code": "timestamp_configuration_missing"})
        effective_times.append(effective)
        if effective in seen:
            duplicate_count += 1
            first = first_by_time[effective]
            conflicting = (first.open, first.high, first.low, first.close, first.tick_volume) != (candle.open, candle.high, candle.low, candle.close, candle.tick_volume)
            issues.append({"index": index, "code": "duplicate_candle", "first_index": first_index_by_time[effective], "conflicting_ohlc": conflicting})
        seen.add(effective)
        first_by_time.setdefault(effective, candle)
        first_index_by_time.setdefault(effective, index)
        if index and effective <= effective_times[index - 1]:
            timestamp_integrity = "OUT_OF_ORDER"
            issues.append({"index": index, "code": "out_of_order"})
        if effective > now + timedelta(seconds=1):
            timestamp_integrity = "FUTURE"
            issues.append({"index": index, "code": "future_timestamp", "time": effective.isoformat()})
        values = (candle.open, candle.high, candle.low, candle.close)
        invalid = any(not math.isfinite(float(value)) for value in values) or any(value <= 0 for value in values)
        invalid = invalid or candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close)
        invalid = invalid or candle.high < candle.low
        if invalid:
            invalid_count += 1
            issues.append({"index": index, "code": "invalid_ohlc"})
    unique_times = sorted(set(effective_times))
    gap_count = 0
    for previous, current in zip(unique_times, unique_times[1:]):
        gap = current - previous
        weekend_closure = previous.weekday() in (3, 4) and current.weekday() == 6 and gap <= timedelta(days=4)
        if gap > interval and not weekend_closure:
            gap_count += 1
            issues.append({"code": "time_gap", "from": previous.isoformat(), "to": current.isoformat()})
    latest = max(effective_times) if effective_times else None
    earliest = min(effective_times) if effective_times else None
    latest_complete = bool(latest and latest + interval <= now)
    complete_count = sum(1 for value in effective_times if value + interval <= now)
    if not candles:
        data_quality = "NO_DATA"
        readiness = "NOT_COLLECTED"
    elif timestamp_integrity in {"FUTURE", "OUT_OF_ORDER", "UNCONFIGURED"} or invalid_count:
        data_quality = "INVALID"
        readiness = "INVALID"
    elif gap_count:
        data_quality = "GAPS_DETECTED"
        readiness = "INCOMPLETE"
    elif complete_count < minimum_history:
        data_quality = "VALID_BUT_SHORT"
        readiness = "INSUFFICIENT_HISTORY"
    elif not latest_complete:
        data_quality = "VALID_LATEST_INCOMPLETE"
        readiness = "READY_WITH_WARNINGS"
    else:
        data_quality = "VALID"
        readiness = "READY"
    return QualityReport(
        symbol=symbol,
        timeframe=timeframe,
        candle_count=len(candles),
        complete_candle_count=complete_count,
        earliest_candle_time=earliest,
        latest_candle_time=latest,
        gap_count=gap_count,
        duplicate_count=duplicate_count,
        invalid_count=invalid_count,
        timestamp_integrity=timestamp_integrity,
        latest_candle_complete=latest_complete,
        data_quality=data_quality,
        readiness=readiness,
        issues=tuple(issues),
    )


@dataclass(frozen=True)
class CollectionPlanItem:
    symbol: str
    timeframe: str
    tier: str
    priority: float
    target_bars: int
    estimated_bytes: int
    priority_reason: str = ""
    target_level: str = "INITIAL"

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class HistoricalDataEngine:
    """Read-only MT5 historical audit and bounded, resumable collection engine."""

    def __init__(self, client: Any, store: MarketDataStore, broker_time: BrokerTimeConfig, project_root: Path | None = None) -> None:
        self.client = client
        self.store = store
        self.broker_time = broker_time
        self.storage_manager = StorageManager((project_root or store.path.parent).resolve())
        self.resource_monitor = ResourceMonitor((project_root or store.path.parent).resolve(), store.path)
        self._classifications: dict[str, dict[str, Any]] = {}
        self._session_resources: list[ResourceSnapshot] = []
        self._session_resource_protection_events = 0

    def classify(self, symbols: Sequence[str] | None = None) -> dict[str, Any]:
        selected = {item.upper() for item in symbols} if symbols else None
        raw_records = self.client.discover_instruments()
        classifications = [
            classify_instrument(record)
            for record in raw_records
            if not selected or str(record.get("name") or "").upper() in selected
        ]
        ranked = sorted(classifications, key=lambda record: priority_for(record), reverse=True)
        tier_one: list[str] = []
        categories_seen: set[str] = set()
        for record in ranked:
            if record["suitability"] != "ELIGIBLE":
                continue
            if record["category"] not in categories_seen:
                tier_one.append(record["symbol"])
                categories_seen.add(record["category"])
        for record in ranked:
            if record["suitability"] == "ELIGIBLE" and len(tier_one) < 12 and record["symbol"] not in tier_one:
                tier_one.append(record["symbol"])
        tier_two_ordered = [
            record["symbol"]
            for record in ranked
            if record["symbol"] not in tier_one and record["suitability"] in {"ELIGIBLE", "ELIGIBLE_WITH_CAUTION"}
        ][:50]
        tier_two = set(tier_two_ordered)
        for record in classifications:
            score = priority_for(record)
            if record["symbol"] in tier_one:
                tier, reason = "TIER1", "Eligible, high priority, and selected for category diversification"
            elif record["symbol"] in tier_two:
                tier, reason = "TIER2", "Potential research value but below the diversified core cutoff"
            else:
                tier, reason = "TIER3", "Deferred pending stronger suitability, data, liquidity, or storage evidence"
            record["priority_score"] = score
            record["priority_tier"] = tier
            record["priority_reason"] = reason
        self._classifications = {record["symbol"]: record for record in classifications}
        self.store.save_instrument_classifications(classifications)
        return self.store.instrument_summary()

    def _save_page_diagnostic(
        self,
        *,
        session_id: str | None = None,
        job_id: int | None,
        symbol: str,
        timeframe: str,
        request: dict[str, Any],
        candles: Sequence[Candle] = (),
        accepted: Sequence[Candle] = (),
        quality: QualityReport | None = None,
        retry_count: int = 0,
        duration_ms: float | None = None,
        result: str = "SUCCESS",
        message: str = "",
        overlap_count: int = 0,
        error: dict[str, Any] | None = None,
    ) -> int:
        raw_times = [_raw_time(candle) for candle in candles]
        try:
            effective_times = [_effective_time(candle, self.broker_time) for candle in candles] if candles else []
        except TimestampConfigurationError:
            effective_times = []
        mt5_error = (error or {}).get("mt5_error") or {}
        return self.store.save_collection_batch_diagnostic({
            "session_id": session_id,
            "job_id": job_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol.upper(),
            "timeframe": timeframe.upper(),
            "request_type": request.get("request_type", "UNKNOWN"),
            "start_position": request.get("start_position"),
            "requested_from": request.get("from"),
            "requested_to": request.get("to"),
            "batch_size": int(request.get("count", len(candles) or 0)),
            "returned_count": len(candles),
            "accepted_count": len(accepted),
            "first_raw_time": raw_times[0].isoformat() if raw_times else None,
            "last_raw_time": raw_times[-1].isoformat() if raw_times else None,
            "first_effective_time": effective_times[0].isoformat() if effective_times else None,
            "last_effective_time": effective_times[-1].isoformat() if effective_times else None,
            "duplicate_count": quality.duplicate_count if quality else 0,
            "overlap_count": overlap_count,
            "gap_count": quality.gap_count if quality else 0,
            "out_of_order_count": sum(1 for issue in (quality.issues if quality else ()) if issue.get("code") == "out_of_order"),
            "forming_count": len(candles) - len(accepted),
            "quality_status": quality.data_quality if quality else ("ERROR" if error else "UNKNOWN"),
            "retry_count": retry_count,
            "duration_ms": duration_ms,
            "mt5_error_code": mt5_error.get("code", mt5_error.get("value", [None])[0] if isinstance(mt5_error.get("value"), list) else None),
            "mt5_error_description": mt5_error.get("description", mt5_error.get("value", [None, None])[1] if isinstance(mt5_error.get("value"), list) and len(mt5_error.get("value")) > 1 else None),
            "result": result,
            "message": message,
            "provenance": {"broker_server": self.broker_time.server_name, "timestamp_source": self.broker_time.source, "pagination_method": "TIME_ANCHORED"},
        })

    def _retrieve_pages(
        self,
        symbol: str,
        timeframe: str,
        bars: int,
        max_batches: int,
        *,
        session_id: str | None = None,
        job_id: int | None = None,
        start_cursor: datetime | None = None,
    ) -> list[tuple[Candle, ...]]:
        pages: list[tuple[Candle, ...]] = []
        cursor = start_cursor
        seen_page_keys: set[datetime] = set()
        for _ in range(max_batches):
            request = {"request_type": "TIME_RANGE" if cursor else "POSITION", "count": bars}
            if cursor is None:
                request["start_position"] = 0
            started = time.perf_counter()
            candles: tuple[Candle, ...] = ()
            retries = 0
            last_error: Exception | None = None
            for attempt in range(3):
                try:
                    if cursor is None:
                        candles = self.client.get_candles_batch(symbol, timeframe, 0, bars)
                    else:
                        candles = self.client.get_candles_before(symbol, timeframe, cursor, bars)
                        interval_minutes = TIMEFRAME_MINUTES[timeframe.upper()]
                        request["to"] = (cursor - timedelta(minutes=interval_minutes)).isoformat()
                        request["from"] = (cursor - timedelta(minutes=interval_minutes * (max(bars * 4, bars + 24) + 1))).isoformat()
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    details = _error_details(exc)
                    raw_error = details.get("mt5_error") or {}
                    value = raw_error.get("value") if isinstance(raw_error, dict) else None
                    code = value[0] if isinstance(value, (list, tuple)) and value else raw_error.get("code") if isinstance(raw_error, dict) else None
                    if attempt >= 2 or code not in TRANSIENT_MT5_ERROR_CODES:
                        break
                    retries += 1
                    time.sleep(0.1 * (attempt + 1))
            duration_ms = (time.perf_counter() - started) * 1000
            if last_error is not None:
                details = _error_details(last_error)
                self._save_page_diagnostic(session_id=session_id, job_id=job_id, symbol=symbol, timeframe=timeframe, request=request, retry_count=retries, duration_ms=duration_ms, result="FAILED", message=str(last_error), error=details)
                raise last_error
            if not candles:
                self._save_page_diagnostic(session_id=session_id, job_id=job_id, symbol=symbol, timeframe=timeframe, request=request, retry_count=retries, duration_ms=duration_ms, result="EMPTY", message="MT5 returned an empty page")
                break
            raw_times = [_raw_time(candle) for candle in candles]
            out_of_order = sum(1 for left, right in zip(raw_times, raw_times[1:]) if right <= left)
            keys = set(raw_times)
            overlap_keys = keys & seen_page_keys
            if cursor is not None:
                overlap_keys.update(value for value in raw_times if value >= cursor)
            overlap = len(overlap_keys)
            quality = validate_candles(symbol, timeframe, candles, self.broker_time)
            try:
                accepted = tuple(candle for candle in candles if _is_complete(candle, timeframe, self.broker_time, datetime.now(timezone.utc)))
            except TimestampConfigurationError as exc:
                self._save_page_diagnostic(session_id=session_id, job_id=job_id, symbol=symbol, timeframe=timeframe, request=request, candles=candles, quality=quality, retry_count=retries, duration_ms=duration_ms, result="REJECTED", message=str(exc))
                raise
            if out_of_order or overlap:
                self._save_page_diagnostic(session_id=session_id, job_id=job_id, symbol=symbol, timeframe=timeframe, request=request, candles=candles, accepted=accepted, quality=quality, retry_count=retries, duration_ms=duration_ms, result="REJECTED", message="Historical page ordering or boundary overlap is ambiguous", overlap_count=overlap)
                raise ValueError(f"Ambiguous historical page for {symbol} {timeframe}: out_of_order={out_of_order}, overlap={overlap}")
            self._save_page_diagnostic(session_id=session_id, job_id=job_id, symbol=symbol, timeframe=timeframe, request=request, candles=candles, accepted=accepted, quality=quality, retry_count=retries, duration_ms=duration_ms)
            pages.append(candles)
            seen_page_keys.update(keys)
            cursor = min(raw_times)
            if len(candles) < bars:
                break
        return pages

    def audit(self, symbols: Sequence[str], timeframes: Sequence[str], bars: int = 200, full_depth: bool = False, max_batches: int = 10, target_level: str = "INITIAL") -> list[QualityReport]:
        reports: list[QualityReport] = []
        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    pages = self._retrieve_pages(symbol, timeframe, bars, max_batches if full_depth else 1)
                    candles = tuple(candle for page in reversed(pages) for candle in page)
                    report = validate_candles(symbol, timeframe, candles, self.broker_time)
                    local_count = self.store.historical_summary_for(symbol, timeframe)["candles"]
                    self.store.save_coverage({
                        **report.to_dict(),
                        "audited_at": datetime.now(timezone.utc).isoformat(),
                        "metadata": {"audit_scope": "time_anchored_history" if full_depth else "latest_batch", "requested_bars": bars, "batches": len(pages), "history_more_available": len(candles) >= bars * (max_batches if full_depth else 1)},
                        "local_candle_count": local_count,
                        "broker_candle_count": len(candles), "target_count": target_for(timeframe, target_level), "target_level": target_level,
                        "source_status": "AVAILABLE" if candles else "NO_HISTORY", "dataset_version": f"{symbol.upper()}-{timeframe.upper()}-{len(candles)}",
                        "research_readiness": research_readiness(local_count, timeframe, report.data_quality, report.gap_count, target_level),
                        "provenance": {"broker_server": self.broker_time.server_name, "timestamp_source": self.broker_time.source, "collector": "HistoricalDataEngine", "full_depth": full_depth, "pagination_method": "TIME_ANCHORED"},
                    })
                except Exception as exc:
                    report = QualityReport(symbol, timeframe, 0, 0, None, None, 0, 0, 0, "ERROR", False, "ERROR", "NOT_COLLECTED", ({"code": "retrieval_error", "message": str(exc)},))
                    self.store.save_coverage({
                        **report.to_dict(), "audited_at": datetime.now(timezone.utc).isoformat(), "error": str(exc), "metadata": {"requested_bars": bars},
                        "target_count": target_for(timeframe, target_level), "target_level": target_level, "source_status": "UNAVAILABLE",
                        "research_readiness": "NOT_COLLECTED", "provenance": {"broker_server": self.broker_time.server_name, "timestamp_source": self.broker_time.source, "collector": "HistoricalDataEngine"},
                    })
                reports.append(report)
        return reports

    def preview(self, symbols: Sequence[str] | None, timeframes: Sequence[str], tier: str = "TIER1", target_bars: int | None = None, target_level: str = "INITIAL", max_symbols: int = 12) -> list[CollectionPlanItem]:
        if not self._classifications:
            self.classify(symbols)
        if not symbols:
            symbols = tuple(record["symbol"] for record in self._classifications.values() if record.get("priority_tier") == tier.upper())[:max_symbols]
        plan: list[CollectionPlanItem] = []
        for symbol in symbols:
            classification = self._classifications.get(symbol.upper())
            if not classification or classification["suitability"] not in {"ELIGIBLE", "ELIGIBLE_WITH_CAUTION"}:
                continue
            priority = priority_for(classification)
            for timeframe in timeframes:
                target = target_bars or target_for(timeframe, target_level)
                plan.append(CollectionPlanItem(symbol.upper(), timeframe.upper(), tier, priority, target, target * 96, classification.get("priority_reason", ""), target_level.upper()))
        return sorted(plan, key=lambda item: (-item.priority, item.symbol, item.timeframe))

    def collect(
        self,
        plan: Sequence[CollectionPlanItem],
        max_batches: int = 1,
        batch_size: int = DEFAULT_BATCH_SIZE,
        rate_limit_seconds: float = 0.05,
        *,
        session_id: str | None = None,
        max_total_candles: int | None = None,
        max_duration_seconds: float | None = None,
        max_storage_growth_bytes: int | None = None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        job_ids = self.store.create_collection_jobs([item.to_dict() for item in plan], now)
        jobs = [job for job in self.store.collection_jobs() if job["job_id"] in set(job_ids)]
        batches = 0
        collected_this_call = 0
        failed_batches = 0
        paused = False
        stop_reason = "COMPLETED_PLAN"
        started_monotonic = time.monotonic()
        initial_storage_bytes = self.storage_manager.scan().sqlite_size_bytes
        for job in jobs:
            if batches >= max_batches or job["status"] in {"COMPLETE", "CANCELLED", "PAUSED"}:
                if batches >= max_batches:
                    stop_reason = "MAX_BATCHES"
                continue
            storage = self.storage_manager.scan()
            resource = self.resource_monitor.capture()
            if session_id:
                self._session_resources.append(resource)
            if resource.protection_state in {"PROTECTION", "CRITICAL"}:
                self.store.update_collection_job(job["job_id"], now, status="PAUSED", last_error=f"Resource protection: {resource.protection_state}")
                self.store.collection_event(job["job_id"], "PAUSED_RESOURCE", "Collection paused at a resource protection threshold", now, resource.to_dict())
                paused = True
                stop_reason = f"RESOURCE_{resource.protection_state}"
                self._session_resource_protection_events += 1
                break
            if storage.disk_used_percent >= 90:
                self.store.update_collection_job(job["job_id"], now, status="PAUSED", last_error="Storage protection threshold reached")
                self.store.collection_event(job["job_id"], "PAUSED_STORAGE", "Collection paused at the 90% disk threshold", now, storage.to_dict())
                paused = True
                stop_reason = "STORAGE_PROTECTION"
                continue
            if max_total_candles is not None and collected_this_call >= max_total_candles:
                stop_reason = "MAX_CANDLES"
                break
            if max_duration_seconds is not None and time.monotonic() - started_monotonic >= max_duration_seconds:
                stop_reason = "MAX_DURATION"
                break
            if max_storage_growth_bytes is not None and storage.sqlite_size_bytes - initial_storage_bytes >= max_storage_growth_bytes:
                stop_reason = "MAX_STORAGE_GROWTH"
                break
            attempt_time = datetime.now(timezone.utc)
            self.store.update_collection_job(job["job_id"], attempt_time, status="RUNNING", attempts=int(job["attempts"]) + 1, last_error=None, last_attempt_at=attempt_time.isoformat(), pagination_method="TIME_ANCHORED")
            try:
                remaining = max(0, int(job["target_bars"]) - int(job["downloaded_bars"]))
                if remaining == 0:
                    self.store.update_collection_job(job["job_id"], attempt_time, status="COMPLETE", last_successful_at=attempt_time.isoformat())
                    continue
                count = min(batch_size, remaining)
                if max_total_candles is not None:
                    count = min(count, max_total_candles - collected_this_call)
                if count <= 0:
                    stop_reason = "MAX_CANDLES"
                    break
                cursor = datetime.fromisoformat(job["next_start_time"]) if job.get("next_start_time") else None
                if cursor is None and int(job["downloaded_bars"]) > 0:
                    bounds = self.store.historical_bounds(job["symbol"], job["timeframe"])
                    if bounds.get("earliest_raw"):
                        cursor = datetime.fromisoformat(str(bounds["earliest_raw"]))
                pages = self._retrieve_pages(job["symbol"], job["timeframe"], count, 1, session_id=session_id, job_id=int(job["job_id"]), start_cursor=cursor)
                if not pages:
                    message = "MT5 returned no candles"
                    self.store.update_collection_job(job["job_id"], attempt_time, status="FAILED", last_error=message)
                    self.store.collection_event(job["job_id"], "FAILED", message, attempt_time, {"symbol": job["symbol"], "timeframe": job["timeframe"], "requested_bars": count})
                    failed_batches += 1
                    continue
                candles = pages[0]
                captured_at = datetime.now(timezone.utc)
                accepted = tuple(candle for candle in candles if _is_complete(candle, job["timeframe"], self.broker_time, captured_at))
                if not accepted:
                    message = "Only forming candles were returned; no research candle was persisted"
                    self.store.update_collection_job(job["job_id"], attempt_time, status="FAILED", last_error=message)
                    self.store.collection_event(job["job_id"], "FORMING_ONLY", message, attempt_time, {"received": len(candles)})
                    continue
                quality = validate_candles(job["symbol"], job["timeframe"], accepted, self.broker_time, captured_at)
                records = []
                collected_at = captured_at.isoformat()
                for candle in accepted:
                    effective = _effective_time(candle, self.broker_time)
                    records.append({
                        "symbol": candle.symbol, "timeframe": candle.timeframe, "raw_time": _raw_time(candle).isoformat(),
                        "effective_time": effective.isoformat(), "open": candle.open, "high": candle.high, "low": candle.low,
                        "close": candle.close, "tick_volume": candle.tick_volume, "spread": candle.spread, "real_volume": candle.real_volume,
                        "quality_status": "VALID" if quality.data_quality == "VALID_LATEST_INCOMPLETE" else quality.data_quality,
                        "quality_reasons": [issue.get("code") for issue in quality.issues], "collected_at": collected_at,
                    })
                saved = self.store.save_historical_candles(records)
                if saved != len(records):
                    message = f"Persistence rejected {len(records) - saved} already-present candle(s); page was not advanced"
                    self.store.update_collection_job(job["job_id"], attempt_time, status="FAILED", last_error=message)
                    self.store.collection_event(job["job_id"], "DUPLICATE_PERSISTENCE", message, attempt_time, {"received": len(candles), "accepted": len(records), "saved": saved})
                    failed_batches += 1
                    continue
                local = self.store.historical_summary_for(job["symbol"], job["timeframe"])
                self.store.save_coverage({
                    **quality.to_dict(),
                    "audited_at": datetime.now(timezone.utc).isoformat(),
                    "local_candle_count": local["candles"],
                    "broker_candle_count": local["candles"],
                    "target_count": int(job["target_bars"]),
                    "target_level": job.get("target_level", "INITIAL"),
                    "source_status": "COLLECTED",
                    "dataset_version": f"{job['symbol']}-{job['timeframe']}-{local['candles']}",
                    "research_readiness": research_readiness(local["candles"], job["timeframe"], quality.data_quality, quality.gap_count, job.get("target_level", "INITIAL")),
                    "metadata": {"audit_scope": "collected_batches", "quality_batch_count": len(candles), "forming_excluded": len(candles) - len(accepted)},
                    "provenance": {"broker_server": self.broker_time.server_name, "timestamp_source": self.broker_time.source, "collector": "HistoricalDataEngine", "collection_job_id": job["job_id"], "pagination_method": "TIME_ANCHORED"},
                })
                downloaded = int(job["downloaded_bars"]) + len(accepted)
                status = "COMPLETE" if downloaded >= int(job["target_bars"]) else "QUEUED"
                next_cursor = min(_raw_time(candle) for candle in candles)
                completed_at = datetime.now(timezone.utc)
                self.store.update_collection_job(job["job_id"], completed_at, downloaded_bars=downloaded, next_start_position=int(job["next_start_position"]) + len(accepted), next_start_time=next_cursor.isoformat(), status=status, last_successful_at=completed_at.isoformat())
                self.store.collection_event(job["job_id"], "BATCH_SAVED", f"Saved {saved} new historical candles; forming candles excluded: {len(candles) - len(accepted)}", completed_at, {"received": len(candles), "accepted": len(accepted), "saved": saved, "quality": quality.to_dict(), "next_start_time": next_cursor.isoformat()})
                batches += 1
                collected_this_call += len(accepted)
                time.sleep(max(0.0, rate_limit_seconds))
            except Exception as exc:
                self.store.update_collection_job(job["job_id"], attempt_time, status="FAILED", last_error=str(exc))
                self.store.collection_event(job["job_id"], "FAILED", str(exc), attempt_time, {"error": _error_details(exc), "symbol": job["symbol"], "timeframe": job["timeframe"]})
                failed_batches += 1
        if failed_batches:
            stop_reason = "FAILED_BATCH"
        return {"job_ids": job_ids, "batches": batches, "failed_batches": failed_batches, "candles_collected": collected_this_call, "paused_for_storage": paused, "stop_reason": stop_reason, "jobs": self.store.collection_jobs(), "historical": self.store.historical_summary()}

    def collect_session(
        self,
        plan: Sequence[CollectionPlanItem],
        *,
        max_batches: int = 4,
        batch_size: int = 50,
        max_total_candles: int = 400,
        max_duration_seconds: float = 300.0,
        max_storage_growth_bytes: int = 16 * 1024 * 1024,
        rate_limit_seconds: float = 0.25,
    ) -> dict[str, Any]:
        session_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        self._session_resources = [self.resource_monitor.capture()]
        self._session_resource_protection_events = 0
        resource_start = self._session_resources[0]
        initial_storage = self.storage_manager.scan().sqlite_size_bytes
        initial_candles = self.store.historical_summary()["candles"]
        symbols = sorted({item.symbol for item in plan})
        timeframes = sorted({item.timeframe for item in plan})
        self.store.start_collection_session({
            "session_id": session_id,
            "started_at": started_at.isoformat(),
            "symbols": symbols,
            "timeframes": timeframes,
            "max_batches": max_batches,
            "max_candles": max_total_candles,
            "max_duration_seconds": max_duration_seconds,
            "max_storage_growth_bytes": max_storage_growth_bytes,
            "initial_candle_count": initial_candles,
            "initial_storage_bytes": initial_storage,
            "resource_start": resource_start.to_dict(),
        })
        try:
            result = self.collect(
                plan,
                max_batches=max_batches,
                batch_size=batch_size,
                rate_limit_seconds=rate_limit_seconds,
                session_id=session_id,
                max_total_candles=max_total_candles,
                max_duration_seconds=max_duration_seconds,
                max_storage_growth_bytes=max_storage_growth_bytes,
            )
            status = "FAILED" if result.get("failed_batches") else ("PAUSED" if result["paused_for_storage"] else "COMPLETED")
            stop_reason = result["stop_reason"]
        except Exception as exc:
            result = {"job_ids": [], "batches": 0, "failed_batches": 1, "candles_collected": 0, "paused_for_storage": False, "stop_reason": "SESSION_ERROR", "error": str(exc), "jobs": self.store.collection_jobs(), "historical": self.store.historical_summary()}
            status = "FAILED"
            stop_reason = str(exc)
        finally:
            resource_end = self.resource_monitor.capture()
            self._session_resources.append(resource_end)
            final_storage = self.storage_manager.scan().sqlite_size_bytes
            peak = max(self._session_resources, key=lambda item: (item.ram_used_percent or 0.0, item.disk_used_percent or 0.0))
            session = self.store.finish_collection_session(session_id, datetime.now(timezone.utc).isoformat(), status, stop_reason, self.store.historical_summary()["candles"], final_storage, peak.process_memory_mb, peak.cpu_percent, resource_end.to_dict(), peak.to_dict(), self._session_resource_protection_events)
        result["session"] = session
        return result


class CollectionScheduler:
    """Persistence-backed control surface; it does not create an OS-level scheduler."""

    def __init__(self, store: MarketDataStore) -> None:
        self.store = store

    def pause(self) -> None:
        now = datetime.now(timezone.utc)
        for job in self.store.collection_jobs(("QUEUED", "RUNNING")):
            self.store.update_collection_job(job["job_id"], now, status="PAUSED")

    def resume(self) -> None:
        now = datetime.now(timezone.utc)
        for job in self.store.collection_jobs(("PAUSED",)):
            self.store.update_collection_job(job["job_id"], now, status="QUEUED", last_error=None)

    def cancel(self) -> None:
        now = datetime.now(timezone.utc)
        for job in self.store.collection_jobs(("QUEUED", "RUNNING", "PAUSED")):
            self.store.update_collection_job(job["job_id"], now, status="CANCELLED")

    def status(self) -> dict[str, Any]:
        jobs = self.store.collection_jobs()
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        return {"active_scheduler": False, "message": "Collection runs only while explicitly started by the CLI or dashboard.", "counts": counts, "jobs": jobs, "sessions": self.store.collection_sessions(), "historical": self.store.historical_summary()}
