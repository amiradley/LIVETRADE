from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .mt5_client import Mt5Error


class FinancialDataError(Mt5Error):
    """Raised when broker financial data is unavailable or invalid."""


def _optional_float(value: Any, label: str) -> float | None:
    if value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise FinancialDataError(f"Invalid broker value for {label}: {value!r}") from exc
    if not math.isfinite(converted):
        raise FinancialDataError(f"Non-finite broker value for {label}")
    return converted


def _optional_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise FinancialDataError(f"Invalid broker timestamp: {value!r}") from exc


@dataclass(frozen=True)
class PositionSnapshot:
    ticket: int | None
    symbol: str
    side: str
    volume: float | None
    entry_price: float | None
    current_price: float | None
    floating_pnl: float | None
    stop_loss: float | None
    take_profit: float | None
    margin: float | None
    opened_at: datetime | None
    captured_at: datetime
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FinancialSnapshot:
    captured_at: datetime
    account_type: str
    account_currency: str
    balance: float | None
    equity: float | None
    margin_used: float | None
    free_margin: float | None
    floating_pnl: float
    positions: tuple[PositionSnapshot, ...]
    data_quality: str


def _side(raw_type: Any) -> str:
    return {0: "BUY", 1: "SELL", "0": "BUY", "1": "SELL"}.get(raw_type, "UNKNOWN")


class FinancialDataService:
    """Build a read-only financial view from MT5 account and positions."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def capture(self, captured_at: datetime | None = None) -> FinancialSnapshot:
        captured_at = captured_at or datetime.now(timezone.utc)
        account = self.client.account_info()
        if account is None:
            raise FinancialDataError("MT5 account information is unavailable")
        raw_positions = self.client.positions()
        positions = tuple(self._position(raw, captured_at) for raw in raw_positions)
        floating_pnl = sum(
            position.floating_pnl for position in positions if position.floating_pnl is not None
        )
        trade_mode = account.get("trade_mode")
        account_type = "DEMO" if trade_mode == 0 else "REAL" if trade_mode == 1 else "UNKNOWN"
        currency = str(account.get("currency") or "UNKNOWN")
        values = {
            "balance": _optional_float(account.get("balance"), "balance"),
            "equity": _optional_float(account.get("equity"), "equity"),
            "margin": _optional_float(account.get("margin"), "margin"),
            "margin_free": _optional_float(account.get("margin_free"), "margin_free"),
        }
        quality = "OK" if values["balance"] is not None and values["equity"] is not None else "INCOMPLETE"
        return FinancialSnapshot(
            captured_at=captured_at.astimezone(timezone.utc),
            account_type=account_type,
            account_currency=currency,
            balance=values["balance"],
            equity=values["equity"],
            margin_used=values["margin"],
            free_margin=values["margin_free"],
            floating_pnl=floating_pnl,
            positions=positions,
            data_quality=quality,
        )

    @staticmethod
    def _position(raw: Mapping[str, Any], captured_at: datetime) -> PositionSnapshot:
        ticket = raw.get("ticket")
        try:
            ticket = int(ticket) if ticket is not None else None
        except (TypeError, ValueError, OverflowError) as exc:
            raise FinancialDataError(f"Invalid position ticket: {ticket!r}") from exc
        return PositionSnapshot(
            ticket=ticket,
            symbol=str(raw.get("symbol") or "UNKNOWN"),
            side=_side(raw.get("type")),
            volume=_optional_float(raw.get("volume"), "position volume"),
            entry_price=_optional_float(raw.get("price_open"), "position entry price"),
            current_price=_optional_float(raw.get("price_current"), "position current price"),
            floating_pnl=_optional_float(raw.get("profit"), "position profit"),
            stop_loss=_optional_float(raw.get("sl"), "position stop loss"),
            take_profit=_optional_float(raw.get("tp"), "position take profit"),
            margin=_optional_float(raw.get("margin"), "position margin"),
            opened_at=_optional_datetime(raw.get("time")),
            captured_at=captured_at.astimezone(timezone.utc),
            raw=raw,
        )
