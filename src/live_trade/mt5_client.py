from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from .models import Candle, MarketSnapshot, Tick

LOGGER = logging.getLogger(__name__)


class Mt5Error(RuntimeError):
    """Base error for read-only MT5 access."""


class Mt5UnavailableError(Mt5Error):
    """Raised when the official MetaTrader5 package is unavailable."""


class Mt5ConnectionError(Mt5Error):
    """Raised when the terminal cannot be initialized."""


def load_native_api() -> ModuleType:
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise Mt5UnavailableError(
            "MetaTrader5 is not installed. Install project dependencies in the project venv."
        ) from exc
    return mt5


def _record_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {"value": repr(value)}


def _utc_from_epoch(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


class ReadOnlyMt5Client:
    """Small adapter exposing only terminal and market-data reads."""

    TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")

    def __init__(self, api: Any | None = None, mt5_path: Path | None = None) -> None:
        self.api = api or load_native_api()
        self.mt5_path = mt5_path
        self._connected = False

    def connect(self) -> None:
        kwargs = {"path": str(self.mt5_path)} if self.mt5_path else {}
        initialized = self.api.initialize(**kwargs)
        if not initialized:
            error = _record_to_dict(self.api.last_error())
            raise Mt5ConnectionError(f"MT5 initialize failed: {error}")
        self._connected = True
        LOGGER.info("Connected to MT5 terminal")

    def disconnect(self) -> None:
        if self._connected:
            self.api.shutdown()
            self._connected = False
            LOGGER.info("Disconnected from MT5 terminal")

    def _require_connection(self) -> None:
        if not self._connected:
            raise Mt5ConnectionError("Connect before requesting market data")

    def terminal_info(self) -> dict[str, Any]:
        self._require_connection()
        return _record_to_dict(self.api.terminal_info())

    def account_info(self) -> dict[str, Any] | None:
        self._require_connection()
        value = self.api.account_info()
        return _record_to_dict(value) if value is not None else None

    def discover_instruments(self) -> tuple[dict[str, Any], ...]:
        self._require_connection()
        values = self.api.symbols_get()
        if values is None:
            error = _record_to_dict(self.api.last_error())
            raise Mt5Error(f"MT5 symbols_get failed: {error}")
        return tuple(_record_to_dict(value) for value in values)

    def get_tick(self, symbol: str) -> Tick:
        self._require_connection()
        value = self.api.symbol_info_tick(symbol)
        if value is None:
            error = _record_to_dict(self.api.last_error())
            raise Mt5Error(f"No tick available for {symbol}: {error}")
        raw = _record_to_dict(value)
        if "time" not in raw or "bid" not in raw or "ask" not in raw:
            raise Mt5Error(f"Tick for {symbol} is missing required fields")
        return Tick(
            symbol=symbol,
            time=_utc_from_epoch(raw["time"]),
            bid=float(raw["bid"]),
            ask=float(raw["ask"]),
            last=float(raw["last"]) if raw.get("last") is not None else None,
            volume=float(raw["volume"]) if raw.get("volume") is not None else None,
            raw=raw,
        )

    def get_candles(self, symbol: str, timeframe: str, count: int) -> tuple[Candle, ...]:
        self._require_connection()
        timeframe = timeframe.upper()
        if timeframe not in self.TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe {timeframe}; use {', '.join(self.TIMEFRAMES)}")
        if not 1 <= count <= 10000:
            raise ValueError("Candle count must be between 1 and 10000")
        constant = getattr(self.api, f"TIMEFRAME_{timeframe}")
        values = self.api.copy_rates_from_pos(symbol, constant, 0, count)
        if values is None:
            error = _record_to_dict(self.api.last_error())
            raise Mt5Error(f"No candles for {symbol} {timeframe}: {error}")
        candles = []
        for value in values:
            raw = _record_to_dict(value)
            required = {"time", "open", "high", "low", "close", "tick_volume"}
            if not required.issubset(raw):
                raise Mt5Error(f"Candle for {symbol} {timeframe} is missing required fields")
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=_utc_from_epoch(raw["time"]),
                    open=float(raw["open"]),
                    high=float(raw["high"]),
                    low=float(raw["low"]),
                    close=float(raw["close"]),
                    tick_volume=int(raw["tick_volume"]),
                    spread=int(raw["spread"]) if raw.get("spread") is not None else None,
                    real_volume=int(raw["real_volume"]) if raw.get("real_volume") is not None else None,
                )
            )
        return tuple(candles)

    def snapshot(self, symbols: tuple[str, ...], timeframe: str, bars: int) -> MarketSnapshot:
        self._require_connection()
        ticks = tuple(self.get_tick(symbol) for symbol in symbols)
        candles = tuple(
            candle
            for symbol in symbols
            for candle in self.get_candles(symbol, timeframe, bars)
        )
        return MarketSnapshot(
            captured_at=datetime.now(timezone.utc),
            terminal_info=self.terminal_info(),
            account_info=self.account_info(),
            instruments=self.discover_instruments(),
            ticks=ticks,
            candles=candles,
        )
