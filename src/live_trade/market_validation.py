from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from .models import Candle, Tick
from .timestamping import BrokerTimeConfig, TimestampConfigurationError


TIMEFRAME_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class MarketDataValidationError(ValueError):
    """Raised when market data is unsafe for a live decision."""


@dataclass(frozen=True)
class MarketDataEvidence:
    symbol: str
    timeframe: str
    captured_at: datetime
    tick_raw_time: datetime
    tick_effective_time: datetime
    tick_age_seconds: float
    source_candle_raw_time: datetime
    source_candle_effective_time: datetime
    source_candle_age_seconds: float
    latest_candle_effective_time: datetime
    candle_count: int
    timestamp_offset_seconds: float
    timestamp_source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "captured_at": self.captured_at.isoformat(),
            "tick_raw_time": self.tick_raw_time.isoformat(),
            "tick_effective_time": self.tick_effective_time.isoformat(),
            "tick_age_seconds": self.tick_age_seconds,
            "source_candle_raw_time": self.source_candle_raw_time.isoformat(),
            "source_candle_effective_time": self.source_candle_effective_time.isoformat(),
            "source_candle_age_seconds": self.source_candle_age_seconds,
            "latest_candle_effective_time": self.latest_candle_effective_time.isoformat(),
            "candle_count": self.candle_count,
            "timestamp_offset_seconds": self.timestamp_offset_seconds,
            "timestamp_source": self.timestamp_source,
        }


def validate_decision_market_data(
    symbol: str,
    timeframe: str,
    tick: Tick,
    candles: Sequence[Candle],
    broker_time: BrokerTimeConfig,
    captured_at: datetime | None = None,
    *,
    max_tick_age_seconds: float = 60.0,
    max_candle_age_seconds: float | None = None,
) -> MarketDataEvidence:
    """Apply broker-time conversion and reject future, stale or incomplete data."""
    timeframe = timeframe.upper()
    if timeframe not in TIMEFRAME_MINUTES:
        raise MarketDataValidationError(f"Unsupported timeframe {timeframe}")
    if not broker_time.configured:
        raise MarketDataValidationError("Broker timestamp configuration is missing")
    now = captured_at or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    interval = timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    candle_age_limit = max_candle_age_seconds if max_candle_age_seconds is not None else interval.total_seconds() * 2
    try:
        tick_effective = broker_time.effective_utc(tick.time)
        effective_times = [broker_time.effective_utc(candle.time) for candle in candles]
    except TimestampConfigurationError as exc:
        raise MarketDataValidationError(str(exc)) from exc
    if not candles:
        raise MarketDataValidationError("No candles are available")
    if len(candles) < 2:
        raise MarketDataValidationError("At least one closed candle and one latest candle are required")
    if tick_effective > now + timedelta(seconds=1):
        raise MarketDataValidationError("Tick timestamp is in the future")
    tick_age = (now - tick_effective).total_seconds()
    if tick_age > max_tick_age_seconds:
        raise MarketDataValidationError(f"Tick is stale by {tick_age:.1f} seconds")
    for previous, current in zip(effective_times, effective_times[1:]):
        if current <= previous:
            raise MarketDataValidationError("Candle timestamps are not strictly increasing")
    if any(value > now + timedelta(seconds=1) for value in effective_times):
        raise MarketDataValidationError("Candle timestamp is in the future")

    # The technical engine excludes the last MT5 record as the forming/latest candle.
    source_index = len(candles) - 2
    source_effective = effective_times[source_index]
    if source_effective + interval > now + timedelta(seconds=1):
        raise MarketDataValidationError("Source candle is not closed")
    source_age = (now - source_effective).total_seconds()
    if source_age > candle_age_limit:
        raise MarketDataValidationError(f"Source candle is stale by {source_age:.1f} seconds")
    return MarketDataEvidence(
        symbol=symbol,
        timeframe=timeframe,
        captured_at=now,
        tick_raw_time=tick.time,
        tick_effective_time=tick_effective,
        tick_age_seconds=tick_age,
        source_candle_raw_time=candles[source_index].time,
        source_candle_effective_time=source_effective,
        source_candle_age_seconds=source_age,
        latest_candle_effective_time=effective_times[-1],
        candle_count=len(candles),
        timestamp_offset_seconds=broker_time.utc_offset_seconds or 0.0,
        timestamp_source=broker_time.source,
    )
