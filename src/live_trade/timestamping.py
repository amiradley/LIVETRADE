from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any


MALAYSIA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Kuala_Lumpur")
MALAYSIA_TIMEZONE_NAME = "Asia/Kuala_Lumpur"


class TimestampConfigurationError(ValueError):
    """Raised when broker time cannot be interpreted safely."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def broker_timestamp_fields(value: datetime | None, broker_time: "BrokerTimeConfig | None") -> dict[str, Any]:
    """Return raw, effective UTC, and Malaysia presentation values.

    The raw value remains the broker/server label captured from MT5. It is never
    rewritten; only the effective and display representations apply the audited
    broker offset at the presentation boundary.
    """
    if value is None:
        return {
            "raw": None,
            "effective_utc": None,
            "display": None,
            "display_timezone": MALAYSIA_TIMEZONE_NAME,
            "status": "UNAVAILABLE",
        }
    raw = _aware(value).isoformat()
    if broker_time is None or not broker_time.configured:
        return {
            "raw": raw,
            "effective_utc": None,
            "display": None,
            "display_timezone": MALAYSIA_TIMEZONE_NAME,
            "status": "UNCONFIGURED",
        }
    effective = broker_time.effective_utc(value)
    return {
        "raw": raw,
        "effective_utc": effective.isoformat(),
        "display": effective.astimezone(MALAYSIA_TIMEZONE).isoformat(),
        "display_timezone": MALAYSIA_TIMEZONE_NAME,
        "status": "OK",
    }


def utc_timestamp_fields(value: datetime | None) -> dict[str, Any]:
    """Return explicit UTC and Malaysia representations for Python timestamps."""
    if value is None:
        return {
            "utc": None,
            "display": None,
            "display_timezone": MALAYSIA_TIMEZONE_NAME,
            "status": "UNAVAILABLE",
        }
    utc = _aware(value).astimezone(timezone.utc)
    return {
        "utc": utc.isoformat(),
        "display": utc.astimezone(MALAYSIA_TIMEZONE).isoformat(),
        "display_timezone": MALAYSIA_TIMEZONE_NAME,
        "status": "OK",
    }


def timestamp_presentation_context(broker_time: "BrokerTimeConfig") -> dict[str, Any]:
    return {
        "display_timezone": MALAYSIA_TIMEZONE_NAME,
        "display_utc_offset_hours": 8,
        "broker_time": broker_time.to_dict(),
        "rule": "Raw MT5 broker labels are preserved; audited broker offset is applied once to derive effective UTC, then displayed in Malaysia time.",
    }


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
