from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any


class TimestampConfigurationError(ValueError):
    """Raised when broker time cannot be interpreted safely."""


@dataclass(frozen=True)
class BrokerTimeConfig:
    """Audited broker-server-to-UTC interpretation for market timestamps."""

    server_name: str
    utc_offset_hours: float | None
    source: str
    verified_at: date | None

    @property
    def utc_offset_seconds(self) -> float | None:
        return self.utc_offset_hours * 3600 if self.utc_offset_hours is not None else None

    @property
    def configured(self) -> bool:
        return self.utc_offset_hours is not None

    def effective_utc(self, broker_timestamp: datetime) -> datetime:
        if self.utc_offset_seconds is None:
            raise TimestampConfigurationError(
                f"No verified UTC offset is configured for broker server {self.server_name}"
            )
        value = broker_timestamp if broker_timestamp.tzinfo else broker_timestamp.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc) - timedelta(seconds=self.utc_offset_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_name": self.server_name,
            "utc_offset_hours": self.utc_offset_hours,
            "utc_offset_seconds": self.utc_offset_seconds,
            "source": self.source,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "configured": self.configured,
        }

    @classmethod
    def metaquotes_demo_default(cls) -> "BrokerTimeConfig":
        return cls(
            server_name="MetaQuotes-Demo",
            utc_offset_hours=2.0,
            source="Verified against MT5 tick and M1 candle epochs on 2026-11-17; explicit project default",
            verified_at=date(2026, 11, 17),
        )

    @classmethod
    def from_environment(cls) -> "BrokerTimeConfig":
        server = os.getenv("LIVE_TRADE_BROKER_SERVER", "MetaQuotes-Demo").strip() or "MetaQuotes-Demo"
        raw_offset = os.getenv("LIVE_TRADE_BROKER_UTC_OFFSET_HOURS")
        if raw_offset is None and server == "MetaQuotes-Demo":
            return cls.metaquotes_demo_default()
        if raw_offset is None or not raw_offset.strip():
            return cls(
                server_name=server,
                utc_offset_hours=None,
                source="Missing explicit broker UTC offset configuration",
                verified_at=None,
            )
        try:
            offset = float(raw_offset)
        except ValueError as exc:
            raise TimestampConfigurationError("LIVE_TRADE_BROKER_UTC_OFFSET_HOURS must be numeric") from exc
        if not math.isfinite(offset) or abs(offset) > 24:
            raise TimestampConfigurationError("LIVE_TRADE_BROKER_UTC_OFFSET_HOURS must be finite and within +/-24 hours")
        verified = os.getenv("LIVE_TRADE_BROKER_TIME_VERIFIED_AT")
        verified_at = date.fromisoformat(verified) if verified else None
        source = os.getenv("LIVE_TRADE_BROKER_TIME_SOURCE", "Explicit environment configuration")
        return cls(server, offset, source, verified_at)

    @classmethod
    def with_offset(cls, offset_hours: float, source: str = "Explicit command configuration") -> "BrokerTimeConfig":
        if not math.isfinite(offset_hours) or abs(offset_hours) > 24:
            raise TimestampConfigurationError("Broker UTC offset must be finite and within +/-24 hours")
        return cls("Configured broker", offset_hours, source, None)
