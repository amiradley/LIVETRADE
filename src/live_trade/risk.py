from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RiskConfigurationError(ValueError):
    """Raised when persistent risk configuration is missing or invalid."""


def _risk_timezone(name: str):
    """Return the configured zone; Malaysia has no DST and needs no tzdata package."""
    if name == "Asia/Kuala_Lumpur":
        return timezone(timedelta(hours=8), name="MYT")
    return ZoneInfo(name)


@dataclass(frozen=True)
class RiskLimitConfiguration:
    """Approved loss limits, stored in RM and converted using a recorded FX rate."""

    daily_loss_limit_myr: float = 4.88
    weekly_loss_limit_myr: float = 9.77
    max_drawdown_myr: float = 19.54
    timezone_name: str = "Asia/Kuala_Lumpur"
    account_currency: str = "USD"
    quote_currency: str = "MYR"
    usd_to_myr: float | None = None
    exchange_rate_source: str | None = None
    exchange_rate_retrieved_at: datetime | None = None
    exchange_rate_status: str = "UNAVAILABLE"
    approved_at: datetime | None = None
    active: bool = True

    def __post_init__(self) -> None:
        for name in ("daily_loss_limit_myr", "weekly_loss_limit_myr", "max_drawdown_myr"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise RiskConfigurationError(f"{name} must be finite and positive")
        if self.usd_to_myr is not None and (not math.isfinite(self.usd_to_myr) or self.usd_to_myr <= 0):
            raise RiskConfigurationError("usd_to_myr must be finite and positive")
        try:
            _risk_timezone(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise RiskConfigurationError(f"Unknown risk timezone: {self.timezone_name}") from exc

    @property
    def conversion_ready(self) -> bool:
        return self.usd_to_myr is not None and self.exchange_rate_status == "OK"

    def usd(self, myr: float) -> float | None:
        return myr / self.usd_to_myr if self.conversion_ready else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "daily_loss_limit_myr": self.daily_loss_limit_myr,
            "weekly_loss_limit_myr": self.weekly_loss_limit_myr,
            "max_drawdown_myr": self.max_drawdown_myr,
            "daily_loss_limit_usd": self.usd(self.daily_loss_limit_myr),
            "weekly_loss_limit_usd": self.usd(self.weekly_loss_limit_myr),
            "max_drawdown_usd": self.usd(self.max_drawdown_myr),
            "timezone": self.timezone_name,
            "account_currency": self.account_currency,
            "quote_currency": self.quote_currency,
            "usd_to_myr": self.usd_to_myr,
            "exchange_rate_source": self.exchange_rate_source,
            "exchange_rate_retrieved_at": self.exchange_rate_retrieved_at.isoformat()
            if self.exchange_rate_retrieved_at
            else None,
            "exchange_rate_status": self.exchange_rate_status,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "active": self.active,
            "methodology": {
                "daily_weekly": "Net closing-deal P&L only; Malaysia local midnight boundaries.",
                "drawdown": "Persisted peak account equity minus current equity; equity includes realised and floating P&L without summing them twice.",
                "rate": "RM values divided by the recorded USD-to-MYR rate; stale or unavailable rates fail closed.",
            },
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "RiskLimitConfiguration":
        def dt(value: Any) -> datetime | None:
            return datetime.fromisoformat(str(value)) if value else None

        return cls(
            daily_loss_limit_myr=float(row["daily_loss_limit_myr"]),
            weekly_loss_limit_myr=float(row["weekly_loss_limit_myr"]),
            max_drawdown_myr=float(row["max_drawdown_myr"]),
            timezone_name=str(row["timezone_name"]),
            account_currency=str(row["account_currency"]),
            quote_currency=str(row["quote_currency"]),
            usd_to_myr=float(row["usd_to_myr"]) if row["usd_to_myr"] is not None else None,
            exchange_rate_source=row["exchange_rate_source"],
            exchange_rate_retrieved_at=dt(row["exchange_rate_retrieved_at"]),
            exchange_rate_status=str(row["exchange_rate_status"]),
            approved_at=dt(row["approved_at"]),
            active=bool(row["active"]),
        )


@dataclass(frozen=True)
class RiskAssessment:
    entries_allowed: bool
    status: str
    reasons: tuple[str, ...]
    configuration: dict[str, Any]
    period: dict[str, Any]
    realized_pnl_usd: float | None
    floating_pnl_usd: float | None
    peak_equity_usd: float | None
    current_equity_usd: float | None
    drawdown_usd: float | None
    events: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries_allowed": self.entries_allowed,
            "status": self.status,
            "reasons": list(self.reasons),
            "configuration": self.configuration,
            "period": self.period,
            "realized_pnl_usd": self.realized_pnl_usd,
            "floating_pnl_usd": self.floating_pnl_usd,
            "peak_equity_usd": self.peak_equity_usd,
            "current_equity_usd": self.current_equity_usd,
            "drawdown_usd": self.drawdown_usd,
            "events": list(self.events),
            "existing_positions_policy": "Existing positions remain under broker-confirmed protective SL/TP; this gate only blocks new entries.",
        }


class PersistentRiskManager:
    """Persisted DEMO loss limits and fail-closed entry assessment."""

    RATE_MAX_AGE = timedelta(days=2)

    def __init__(self, store: Any, broker_offset_seconds: float = 0.0) -> None:
        self.store = store
        self.broker_offset_seconds = broker_offset_seconds

    def ensure_configuration(self, now: datetime | None = None) -> RiskLimitConfiguration | None:
        existing = self.store.risk_configuration()
        if existing:
            return RiskLimitConfiguration.from_row(existing)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        rate = self.store.latest_exchange_rate("USD", "MYR")
        if not rate or rate.get("status") != "OK" or rate.get("rate") is None:
            return None
        retrieved_at = datetime.fromisoformat(str(rate["retrieved_at"])).astimezone(timezone.utc)
        configuration = RiskLimitConfiguration(
            usd_to_myr=float(rate["rate"]),
            exchange_rate_source=str(rate.get("source") or "recorded exchange rate"),
            exchange_rate_retrieved_at=retrieved_at,
            exchange_rate_status=str(rate["status"]),
            approved_at=now,
        )
        self.store.save_risk_configuration(configuration.to_dict())
        return configuration

    def _periods(self, now: datetime, config: RiskLimitConfiguration) -> tuple[datetime, datetime, str]:
        local = now.astimezone(_risk_timezone(config.timezone_name))
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=local.weekday())
        return day_start.astimezone(timezone.utc), week_start.astimezone(timezone.utc), local.isoformat()

    def assess(self, account: Mapping[str, Any] | None = None, now: datetime | None = None) -> RiskAssessment:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        config = self.ensure_configuration(now)
        reasons: list[str] = []
        if config is None:
            return RiskAssessment(False, "BLOCKED", ("risk_configuration_or_exchange_rate_unavailable",), {}, {}, None, None, None, None, None, ())
        configuration = config.to_dict()
        if not config.active:
            reasons.append("risk_configuration_inactive")
        if not config.conversion_ready:
            reasons.append("risk_exchange_rate_invalid_or_stale")
        elif config.exchange_rate_retrieved_at is None or now - config.exchange_rate_retrieved_at > self.RATE_MAX_AGE:
            reasons.append("risk_exchange_rate_stale")
        if account is None:
            reasons.append("live_account_risk_data_unavailable")
            return RiskAssessment(False, "BLOCKED", tuple(reasons), configuration, {}, None, None, None, None, None, ())
        try:
            balance = float(account["balance"])
            equity = float(account["equity"])
            if not math.isfinite(balance) or not math.isfinite(equity):
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            reasons.append("invalid_live_account_risk_data")
            return RiskAssessment(False, "BLOCKED", tuple(reasons), configuration, {}, None, None, None, None, None, ())

        day_start, week_start, local_now = self._periods(now, config)
        state = self.store.risk_state()
        baseline = float(state["baseline_equity_usd"]) if state and state.get("baseline_equity_usd") is not None else equity
        peak = max(float(state["peak_equity_usd"]) if state and state.get("peak_equity_usd") is not None else baseline, equity)
        self.store.save_risk_state({
            "baseline_equity_usd": baseline,
            "peak_equity_usd": peak,
            "captured_at": now.isoformat(),
        })
        daily_net, daily_count = self.store.realized_pnl_between(day_start, now, self.broker_offset_seconds)
        weekly_net, weekly_count = self.store.realized_pnl_between(week_start, now, self.broker_offset_seconds)
        floating = equity - balance
        drawdown = max(0.0, peak - equity)
        daily_loss = max(0.0, -daily_net)
        weekly_loss = max(0.0, -weekly_net)
        checks = (
            ("daily_realised_loss_limit_reached", daily_loss, config.usd(config.daily_loss_limit_myr), day_start),
            ("weekly_realised_loss_limit_reached", weekly_loss, config.usd(config.weekly_loss_limit_myr), week_start),
            ("maximum_drawdown_reached", drawdown, config.usd(config.max_drawdown_myr), None),
        )
        events: list[dict[str, Any]] = []
        for name, value, threshold, period_start in checks:
            if threshold is None:
                continue
            if value >= threshold:
                reasons.append(name)
                event = {
                    "limit_name": name,
                    "period_start": period_start.isoformat() if period_start else "CUMULATIVE",
                    "triggered_at": now.isoformat(),
                    "value_usd": value,
                    "limit_usd": threshold,
                    "daily_net_usd": daily_net,
                    "weekly_net_usd": weekly_net,
                    "floating_pnl_usd": floating,
                    "peak_equity_usd": peak,
                    "current_equity_usd": equity,
                    "source_deal_count": daily_count if "daily" in name else weekly_count if "weekly" in name else None,
                }
                self.store.save_risk_event(event)
                events.append(event)
        period = {
            "timezone": config.timezone_name,
            "local_now": local_now,
            "daily_start_utc": day_start.astimezone(timezone.utc).isoformat(),
            "weekly_start_utc": week_start.astimezone(timezone.utc).isoformat(),
            "daily_net_usd": daily_net,
            "weekly_net_usd": weekly_net,
            "daily_loss_usd": daily_loss,
            "weekly_loss_usd": weekly_loss,
            "daily_deal_count": daily_count,
            "weekly_deal_count": weekly_count,
        }
        status = "BLOCKED" if reasons else "PASS"
        return RiskAssessment(not reasons, status, tuple(reasons), configuration, period, daily_net, floating, peak, equity, drawdown, tuple(events))
