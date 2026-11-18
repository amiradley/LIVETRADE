from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .models import Candle, Tick
from .timestamping import BrokerTimeConfig


SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


def market_session(at: datetime) -> str:
    """Return a coarse UTC session label; broker-specific hours come later."""
    at = at.astimezone(timezone.utc)
    if at.weekday() >= 5:
        return "CLOSED_WEEKEND"
    if at.hour < 7:
        return "ASIA_PACIFIC"
    if at.hour < 13:
        return "EUROPE"
    if at.hour < 17:
        return "LONDON_NEW_YORK_OVERLAP"
    if at.hour < 22:
        return "NEW_YORK"
    return "ASIA_PACIFIC"


@dataclass(frozen=True)
class ScannerConfig:
    timeframes: tuple[str, ...] = SUPPORTED_TIMEFRAMES
    required_timeframes: tuple[str, ...] = ("M1",)
    bars: int = 100
    max_active_instruments: int = 50
    max_spread_points: float = 30.0
    max_tick_age_seconds: float = 60.0
    min_candles: int = 1
    broker_server_utc_offset_seconds: float = 0.0
    broker_time: BrokerTimeConfig | None = None
    timestamp_configured: bool = False
    timestamp_config_source: str = "Unconfigured scanner; future timestamps remain rejected"

    def __post_init__(self) -> None:
        normalized = tuple(timeframe.upper() for timeframe in self.timeframes)
        required = tuple(timeframe.upper() for timeframe in self.required_timeframes)
        if not normalized or any(timeframe not in SUPPORTED_TIMEFRAMES for timeframe in normalized):
            raise ValueError(f"timeframes must use {', '.join(SUPPORTED_TIMEFRAMES)}")
        if any(timeframe not in normalized for timeframe in required):
            raise ValueError("required_timeframes must be included in timeframes")
        if not 1 <= self.bars <= 10000:
            raise ValueError("bars must be between 1 and 10000")
        if self.max_active_instruments < 1:
            raise ValueError("max_active_instruments must be positive")
        if self.max_spread_points < 0 or self.max_tick_age_seconds < 0:
            raise ValueError("spread and tick-age limits must not be negative")
        if not math.isfinite(self.broker_server_utc_offset_seconds):
            raise ValueError("broker_server_utc_offset_seconds must be finite")
        if self.min_candles < 1:
            raise ValueError("min_candles must be positive")
        object.__setattr__(self, "timeframes", normalized)
        object.__setattr__(self, "required_timeframes", required)
        if self.broker_time is not None:
            offset = self.broker_time.utc_offset_seconds
            object.__setattr__(self, "timestamp_configured", offset is not None)
            object.__setattr__(self, "timestamp_config_source", self.broker_time.source)
            if offset is not None:
                object.__setattr__(self, "broker_server_utc_offset_seconds", offset)
        elif self.broker_server_utc_offset_seconds != 0:
            object.__setattr__(self, "timestamp_configured", True)
            object.__setattr__(self, "timestamp_config_source", "Explicit scanner offset")


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    captured_at: datetime
    tick_time: datetime | None
    bid: float | None
    ask: float | None
    spread_points: float | None
    tick_age_seconds: float | None
    session: str
    eligible: bool
    activated: bool
    effective_tick_time: datetime | None = None
    timestamp_offset_seconds: float = 0.0
    timestamp_configured: bool = False
    timestamp_config_source: str = "Unconfigured scanner; future timestamps remain rejected"
    reasons: tuple[str, ...] = ()
    candle_counts: Mapping[str, int] = field(default_factory=dict)
    latest_candle_times: Mapping[str, datetime] = field(default_factory=dict)


@dataclass(frozen=True)
class ScanRun:
    started_at: datetime
    completed_at: datetime
    discovered_count: int
    results: tuple[ScanResult, ...]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _point_size(instrument: Mapping[str, Any]) -> float | None:
    raw_point = instrument.get("point")
    if raw_point is None and instrument.get("digits") is not None:
        try:
            raw_point = 10 ** -int(instrument["digits"])
        except (TypeError, ValueError, OverflowError):
            return None
    try:
        point = float(raw_point)
    except (TypeError, ValueError, OverflowError):
        return None
    return point if math.isfinite(point) and point > 0 else None


class MarketScanner:
    """Read-only market scanner with deterministic eligibility gates."""

    def __init__(self, client: Any, config: ScannerConfig | None = None) -> None:
        self.client = client
        self.config = config or ScannerConfig()

    def _base_result(
        self,
        instrument: Mapping[str, Any],
        tick: Tick | None,
        captured_at: datetime,
        error: str | None = None,
    ) -> ScanResult:
        symbol = str(instrument.get("name") or instrument.get("symbol") or "UNKNOWN")
        session = market_session(captured_at)
        reasons: list[str] = []
        if error:
            reasons.append(error)
        if instrument.get("visible") is False:
            reasons.append("not_visible")
        if instrument.get("trade_mode") == 0:
            reasons.append("trading_disabled")
        if session == "CLOSED_WEEKEND":
            reasons.append("market_weekend")
        if tick is None:
            return ScanResult(
                symbol=symbol,
                captured_at=captured_at,
                tick_time=None,
                bid=None,
                ask=None,
                spread_points=None,
                tick_age_seconds=None,
                session=session,
                eligible=False,
                activated=False,
                timestamp_offset_seconds=self.config.broker_server_utc_offset_seconds,
                timestamp_configured=self.config.timestamp_configured,
                timestamp_config_source=self.config.timestamp_config_source,
                reasons=tuple(reasons or ["tick_unavailable"]),
            )

        bid = float(tick.bid)
        ask = float(tick.ask)
        if not math.isfinite(bid) or not math.isfinite(ask):
            reasons.append("invalid_bid_ask")
        if ask < bid:
            reasons.append("negative_spread")
        point = _point_size(instrument)
        spread_points = None
        if point is None:
            reasons.append("missing_point_size")
        elif math.isfinite(bid) and math.isfinite(ask) and ask >= bid:
            spread_points = (ask - bid) / point
            if spread_points > self.config.max_spread_points:
                reasons.append("spread_above_limit")
        raw_tick_time = _utc(tick.time)
        if not self.config.timestamp_configured and self.config.broker_time is not None:
            reasons.append("timestamp_configuration_missing")
        effective_tick_time = raw_tick_time - timedelta(
            seconds=self.config.broker_server_utc_offset_seconds
        )
        age = (captured_at - effective_tick_time).total_seconds()
        if age < -1:
            reasons.append("future_tick")
        elif age > self.config.max_tick_age_seconds:
            reasons.append("stale_tick")
        return ScanResult(
            symbol=symbol,
            captured_at=captured_at,
            tick_time=raw_tick_time,
            bid=bid,
            ask=ask,
            spread_points=spread_points,
            tick_age_seconds=age,
            session=session,
            eligible=not reasons,
            activated=False,
            effective_tick_time=effective_tick_time,
            timestamp_offset_seconds=self.config.broker_server_utc_offset_seconds,
            timestamp_configured=self.config.timestamp_configured,
            timestamp_config_source=self.config.timestamp_config_source,
            reasons=tuple(reasons),
        )

    def _load_candles(self, result: ScanResult) -> ScanResult:
        counts: dict[str, int] = {}
        latest: dict[str, datetime] = {}
        reasons = list(result.reasons)
        for timeframe in self.config.timeframes:
            try:
                candles: tuple[Candle, ...] = self.client.get_candles(
                    result.symbol, timeframe, self.config.bars
                )
            except Exception:
                counts[timeframe] = 0
                reasons.append(f"candles_unavailable_{timeframe}")
                continue
            counts[timeframe] = len(candles)
            if candles:
                latest[timeframe] = max(candle.time for candle in candles)
            if timeframe in self.config.required_timeframes and len(candles) < self.config.min_candles:
                reasons.append(f"insufficient_candles_{timeframe}")
        return replace(
            result,
            eligible=not reasons,
            reasons=tuple(reasons),
            candle_counts=counts,
            latest_candle_times=latest,
        )

    def scan(
        self,
        symbols: Sequence[str] | None = None,
        captured_at: datetime | None = None,
    ) -> ScanRun:
        started_at = _utc(captured_at or datetime.now(timezone.utc))
        selected = {symbol.upper() for symbol in symbols} if symbols else None
        instruments = self.client.discover_instruments()
        results: list[ScanResult] = []
        for instrument in instruments:
            symbol = str(instrument.get("name") or instrument.get("symbol") or "")
            if not symbol or (selected is not None and symbol.upper() not in selected):
                continue
            try:
                tick = self.client.get_tick(symbol)
                result = self._base_result(instrument, tick, started_at)
            except Exception:
                result = self._base_result(instrument, None, started_at, "tick_unavailable")
            results.append(result)

        ranked = sorted(
            (result for result in results if result.eligible),
            key=lambda result: (result.spread_points if result.spread_points is not None else float("inf"), result.symbol),
        )
        activation_symbols = {result.symbol for result in ranked[: self.config.max_active_instruments]}
        final_results: list[ScanResult] = []
        for result in results:
            if result.symbol not in activation_symbols:
                final_results.append(result)
                continue
            loaded = self._load_candles(result)
            final_results.append(replace(loaded, activated=loaded.eligible))
        return ScanRun(
            started_at=started_at,
            completed_at=datetime.now(timezone.utc),
            discovered_count=len(instruments),
            results=tuple(final_results),
        )
