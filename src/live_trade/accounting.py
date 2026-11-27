from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .timestamping import BrokerTimeConfig, broker_timestamp_fields


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        converted = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return converted if math.isfinite(converted) else None


def _integer(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _timestamp(raw: Mapping[str, Any]) -> datetime | None:
    value = raw.get("time_msc")
    if value is not None:
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    value = raw.get("time")
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _side(raw_type: Any) -> str:
    return {0: "BUY", 1: "SELL", "0": "BUY", "1": "SELL"}.get(raw_type, "UNKNOWN")


@dataclass(frozen=True)
class BrokerDeal:
    deal_id: int
    order_id: int | None
    position_id: int | None
    symbol: str
    side: str
    entry_type: int | None
    volume: float | None
    price: float | None
    profit: float | None
    commission: float | None
    swap: float | None
    fee: float | None
    deal_time: datetime | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalTrade:
    position_id: int | None
    symbol: str
    side: str
    volume: float | None
    opened_at: datetime | None
    closed_at: datetime | None
    entry_price: float | None
    exit_price: float | None
    gross_profit: float | None
    commission: float | None
    swap: float | None
    fees: float | None
    net_profit: float | None
    deal_ids: tuple[int, ...]
    data_quality: str

    def to_dict(self, broker_time: BrokerTimeConfig | None = None) -> dict[str, Any]:
        opened = broker_timestamp_fields(self.opened_at, broker_time)
        closed = broker_timestamp_fields(self.closed_at, broker_time)
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "volume": self.volume,
            "opened_at": opened["display"] if opened["status"] == "OK" else opened["raw"],
            "closed_at": closed["display"] if closed["status"] == "OK" else closed["raw"],
            "opened_at_raw": opened["raw"],
            "opened_at_utc": opened["effective_utc"],
            "closed_at_raw": closed["raw"],
            "closed_at_utc": closed["effective_utc"],
            "timestamp_status": "OK" if opened["status"] == closed["status"] == "OK" else "UNCONFIGURED",
            "display_timezone": opened["display_timezone"],
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "gross_profit": self.gross_profit,
            "commission": self.commission,
            "swap": self.swap,
            "fees": self.fees,
            "net_profit": self.net_profit,
            "deal_ids": list(self.deal_ids),
            "data_quality": self.data_quality,
        }


def normalize_deal(raw: Mapping[str, Any]) -> BrokerDeal | None:
    deal_id = _integer(raw.get("ticket", raw.get("deal")))
    if deal_id is None:
        return None
    return BrokerDeal(
        deal_id=deal_id,
        order_id=_integer(raw.get("order")),
        position_id=_integer(raw.get("position_id")),
        symbol=str(raw.get("symbol") or "UNKNOWN"),
        side=_side(raw.get("type")),
        entry_type=_integer(raw.get("entry")),
        volume=_number(raw.get("volume")),
        price=_number(raw.get("price")),
        profit=_number(raw.get("profit")),
        commission=_number(raw.get("commission")),
        swap=_number(raw.get("swap")),
        fee=_number(raw.get("fee")),
        deal_time=_timestamp(raw),
        raw=raw,
    )


def _sum(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def aggregate_closed_trades(raw_deals: Sequence[Mapping[str, Any]]) -> tuple[HistoricalTrade, ...]:
    normalized = [deal for raw in raw_deals if (deal := normalize_deal(raw)) is not None]
    groups: dict[tuple[str, int], list[BrokerDeal]] = {}
    for deal in normalized:
        key = (deal.symbol, deal.position_id if deal.position_id is not None else deal.deal_id)
        groups.setdefault(key, []).append(deal)

    trades: list[HistoricalTrade] = []
    for (_, fallback_id), deals in groups.items():
        ordered = sorted(deals, key=lambda deal: (deal.deal_time or datetime.min.replace(tzinfo=timezone.utc), deal.deal_id))
        closing = [deal for deal in ordered if deal.entry_type in (1, 3)]
        if not closing:
            continue
        opening = [deal for deal in ordered if deal.entry_type in (0, 2)]
        opening = opening or ordered[:1]
        entry = opening[0]
        exit_deal = closing[-1]
        gross_profit = _sum([deal.profit for deal in ordered])
        commission = _sum([deal.commission for deal in ordered])
        swap = _sum([deal.swap for deal in ordered])
        fees = _sum([deal.fee for deal in ordered])
        costs_complete = all(value is not None for value in (commission, swap, fees))
        net_profit = (
            gross_profit + commission + swap + fees
            if gross_profit is not None and costs_complete
            else None
        )
        quality = "OK" if net_profit is not None and entry.deal_time is not None and exit_deal.deal_time is not None else "INCOMPLETE"
        trades.append(
            HistoricalTrade(
                position_id=entry.position_id if entry.position_id is not None else fallback_id,
                symbol=entry.symbol,
                side=entry.side,
                volume=_sum([deal.volume for deal in opening]),
                opened_at=entry.deal_time,
                closed_at=exit_deal.deal_time,
                entry_price=entry.price,
                exit_price=exit_deal.price,
                gross_profit=gross_profit,
                commission=commission,
                swap=swap,
                fees=fees,
                net_profit=net_profit,
                deal_ids=tuple(deal.deal_id for deal in ordered),
                data_quality=quality,
            )
        )
    return tuple(sorted(trades, key=lambda trade: trade.closed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True))


def performance_summary(trades: Sequence[HistoricalTrade], now: datetime | None = None) -> dict[str, float | None]:
    now = _utc(now or datetime.now(timezone.utc))
    today = now.date()
    week_start = now - timedelta(days=7)
    complete = [trade for trade in trades if trade.net_profit is not None]
    cumulative = sum(trade.net_profit for trade in complete) if complete else 0.0
    daily = sum(trade.net_profit for trade in complete if trade.closed_at and _utc(trade.closed_at).date() == today)
    weekly = sum(trade.net_profit for trade in complete if trade.closed_at and _utc(trade.closed_at) >= week_start)
    return {"daily": daily, "weekly": weekly, "cumulative": cumulative}


def savings_preview(cumulative_net_profit: float, prior_high_water_mark: float, savings_rate: float = 0.20) -> dict[str, float | str]:
    cumulative_net_profit = float(cumulative_net_profit)
    prior_high_water_mark = max(0.0, float(prior_high_water_mark))
    eligible_profit = max(0.0, cumulative_net_profit - prior_high_water_mark)
    savings_amount = eligible_profit * savings_rate
    retained_amount = eligible_profit - savings_amount
    status = "ALLOCATED" if eligible_profit > 1e-9 else "NO_NEW_ELIGIBLE_PROFIT"
    return {
        "prior_high_water_mark": prior_high_water_mark,
        "cumulative_net_profit": cumulative_net_profit,
        "eligible_profit": eligible_profit,
        "savings_rate": savings_rate,
        "savings_amount": savings_amount,
        "retained_amount": retained_amount,
        "status": status,
    }
