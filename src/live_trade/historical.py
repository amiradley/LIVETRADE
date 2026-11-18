from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .instrument_intelligence import classify_instrument, priority_for
from .models import Candle
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
            issues.append({"index": index, "code": "duplicate_candle"})
        seen.add(effective)
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
        self._classifications: dict[str, dict[str, Any]] = {}

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

    def audit(self, symbols: Sequence[str], timeframes: Sequence[str], bars: int = 200, full_depth: bool = False, max_batches: int = 10, target_level: str = "INITIAL") -> list[QualityReport]:
        reports: list[QualityReport] = []
        for symbol in symbols:
            for timeframe in timeframes:
                try:
                    if full_depth:
                        batches = []
                        for batch_number in range(max_batches):
                            batch = self.client.get_candles_batch(symbol, timeframe, batch_number * bars, bars)
                            if not batch:
                                break
                            batches.append(batch)
                            if len(batch) < bars:
                                break
                        candles = tuple(candle for batch in reversed(batches) for candle in batch)
                    else:
                        candles = self.client.get_candles(symbol, timeframe, bars)
                    report = validate_candles(symbol, timeframe, candles, self.broker_time)
                    local_count = self.store.historical_summary_for(symbol, timeframe)["candles"]
                    self.store.save_coverage({
                        **report.to_dict(),
                        "audited_at": datetime.now(timezone.utc).isoformat(),
                        "metadata": {"audit_scope": "paged_history" if full_depth else "latest_batch", "requested_bars": bars, "batches": max_batches if full_depth else 1, "history_more_available": len(candles) >= bars * (max_batches if full_depth else 1)},
                        "local_candle_count": local_count,
                        "broker_candle_count": len(candles), "target_count": target_for(timeframe, target_level), "target_level": target_level,
                        "source_status": "AVAILABLE" if candles else "NO_HISTORY", "dataset_version": f"{symbol.upper()}-{timeframe.upper()}-{len(candles)}",
                        "research_readiness": research_readiness(local_count, timeframe, report.data_quality, report.gap_count, target_level),
                        "provenance": {"broker_server": self.broker_time.server_name, "timestamp_source": self.broker_time.source, "collector": "HistoricalDataEngine", "full_depth": full_depth},
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

    def collect(self, plan: Sequence[CollectionPlanItem], max_batches: int = 1, batch_size: int = DEFAULT_BATCH_SIZE, rate_limit_seconds: float = 0.05) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        job_ids = self.store.create_collection_jobs([item.to_dict() for item in plan], now)
        jobs = self.store.collection_jobs()
        batches = 0
        paused = False
        for job in jobs:
            if batches >= max_batches or job["status"] in {"COMPLETE", "CANCELLED", "PAUSED"}:
                continue
            storage = self.storage_manager.scan()
            if storage.disk_used_percent >= 90:
                self.store.update_collection_job(job["job_id"], now, status="PAUSED", last_error="Storage protection threshold reached")
                self.store.collection_event(job["job_id"], "PAUSED_STORAGE", "Collection paused at the 90% disk threshold", now, storage.to_dict())
                paused = True
                continue
            self.store.update_collection_job(job["job_id"], now, status="RUNNING", attempts=int(job["attempts"]) + 1, last_error=None)
            try:
                remaining = max(0, int(job["target_bars"]) - int(job["downloaded_bars"]))
                if remaining == 0:
                    self.store.update_collection_job(job["job_id"], now, status="COMPLETE")
                    continue
                count = min(batch_size, remaining)
                candles = self.client.get_candles_batch(job["symbol"], job["timeframe"], int(job["next_start_position"]), count)
                if not candles:
                    self.store.update_collection_job(job["job_id"], now, status="FAILED", last_error="MT5 returned no candles")
                    self.store.collection_event(job["job_id"], "FAILED", "MT5 returned no candles", now)
                    continue
                quality = validate_candles(job["symbol"], job["timeframe"], candles, self.broker_time)
                records = []
                collected_at = datetime.now(timezone.utc).isoformat()
                for candle in candles:
                    effective = self.broker_time.effective_utc(_utc(candle.time))
                    records.append({
                        "symbol": candle.symbol, "timeframe": candle.timeframe, "raw_time": _utc(candle.time).isoformat(),
                        "effective_time": effective.isoformat(), "open": candle.open, "high": candle.high, "low": candle.low,
                        "close": candle.close, "tick_volume": candle.tick_volume, "spread": candle.spread, "real_volume": candle.real_volume,
                        "quality_status": quality.data_quality, "quality_reasons": [issue.get("code") for issue in quality.issues], "collected_at": collected_at,
                    })
                saved = self.store.save_historical_candles(records)
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
                    "metadata": {"audit_scope": "collected_batches", "quality_batch_count": len(candles)},
                    "provenance": {"broker_server": self.broker_time.server_name, "timestamp_source": self.broker_time.source, "collector": "HistoricalDataEngine", "collection_job_id": job["job_id"]},
                })
                downloaded = int(job["downloaded_bars"]) + len(candles)
                status = "COMPLETE" if downloaded >= int(job["target_bars"]) else "QUEUED"
                self.store.update_collection_job(job["job_id"], now, downloaded_bars=downloaded, next_start_position=int(job["next_start_position"]) + len(candles), status=status)
                self.store.collection_event(job["job_id"], "BATCH_SAVED", f"Saved {saved} new historical candles", now, {"received": len(candles), "saved": saved, "quality": quality.to_dict()})
                batches += 1
                time.sleep(max(0.0, rate_limit_seconds))
            except Exception as exc:
                self.store.update_collection_job(job["job_id"], now, status="FAILED", last_error=str(exc))
                self.store.collection_event(job["job_id"], "FAILED", str(exc), now)
        return {"job_ids": job_ids, "batches": batches, "paused_for_storage": paused, "jobs": self.store.collection_jobs(), "historical": self.store.historical_summary()}


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
        return {"active_scheduler": False, "message": "Collection runs only while explicitly started by the CLI or dashboard.", "counts": counts, "jobs": jobs, "historical": self.store.historical_summary()}
