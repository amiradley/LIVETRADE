from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any
from collections.abc import Mapping

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


def _python_scalar(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    return value


def _record_to_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "_asdict"):
        return {key: _python_scalar(item) for key, item in value._asdict().items()}
    if isinstance(value, Mapping):
        return {key: _python_scalar(item) for key, item in value.items()}
    dtype = getattr(value, "dtype", None)
    field_names = getattr(dtype, "names", None)
    if field_names:
        return {name: _python_scalar(value[name]) for name in field_names}
    try:
        return {key: _python_scalar(item) for key, item in dict(value).items()}
    except (TypeError, ValueError):
        return {"value": repr(value)}


def _utc_from_epoch(value: Any) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (OverflowError, OSError, TypeError, ValueError) as exc:
        raise Mt5Error(f"Invalid epoch timestamp: {value!r}") from exc


def _finite_float(raw: Mapping[str, Any], field: str, context: str) -> float:
    if field not in raw or raw[field] is None:
        raise Mt5Error(f"{context} is missing required field {field}")
    try:
        value = float(raw[field])
    except (TypeError, ValueError, OverflowError) as exc:
        raise Mt5Error(f"{context} has invalid numeric field {field}: {raw[field]!r}") from exc
    if not math.isfinite(value):
        raise Mt5Error(f"{context} has non-finite numeric field {field}: {raw[field]!r}")
    return value


def _integer_value(raw: Mapping[str, Any], field: str, context: str, optional: bool = False) -> int | None:
    if field not in raw or raw[field] is None:
        if optional:
            return None
        raise Mt5Error(f"{context} is missing required field {field}")
    try:
        value = int(raw[field])
    except (TypeError, ValueError, OverflowError) as exc:
        raise Mt5Error(f"{context} has invalid integer field {field}: {raw[field]!r}") from exc
    return value


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
            context = f"Candle for {symbol} {timeframe}"
            candles.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=_utc_from_epoch(raw["time"]),
                    open=_finite_float(raw, "open", context),
                    high=_finite_float(raw, "high", context),
                    low=_finite_float(raw, "low", context),
                    close=_finite_float(raw, "close", context),
                    tick_volume=_integer_value(raw, "tick_volume", context),
                    spread=_integer_value(raw, "spread", context, optional=True),
                    real_volume=_integer_value(raw, "real_volume", context, optional=True),
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
