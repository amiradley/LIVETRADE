from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int | None = None
    real_volume: int | None = None


@dataclass(frozen=True)
class Tick:
    symbol: str
    time: datetime
    bid: float
    ask: float
    last: float | None
    volume: float | None
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MarketSnapshot:
    captured_at: datetime
    terminal_info: Mapping[str, Any]
    account_info: Mapping[str, Any] | None
    instruments: tuple[Mapping[str, Any], ...]
    ticks: tuple[Tick, ...]
    candles: tuple[Candle, ...]
