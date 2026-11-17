from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _optional_path(value: str | None, base_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else base_dir / path


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    mt5_path: Path | None
    db_path: Path
    log_path: Path
    symbols: tuple[str, ...]
    default_bars: int = 200


def load_settings(base_dir: Path | None = None) -> Settings:
    root = (base_dir or Path.cwd()).resolve()
    db_path = _optional_path(os.getenv("LIVE_TRADE_DB_PATH", "data/market_data.sqlite3"), root)
    log_path = _optional_path(os.getenv("LIVE_TRADE_LOG_PATH", "logs/live_trade.jsonl"), root)
    symbols = tuple(
        item.strip().upper()
        for item in os.getenv("LIVE_TRADE_SYMBOLS", "").split(",")
        if item.strip()
    )
    try:
        default_bars = int(os.getenv("LIVE_TRADE_DEFAULT_BARS", "200"))
    except ValueError as exc:
        raise ValueError("LIVE_TRADE_DEFAULT_BARS must be an integer") from exc
    if not 1 <= default_bars <= 10000:
        raise ValueError("LIVE_TRADE_DEFAULT_BARS must be between 1 and 10000")
    assert db_path is not None
    assert log_path is not None
    return Settings(
        base_dir=root,
        mt5_path=_optional_path(os.getenv("MT5_PATH"), root),
        db_path=db_path,
        log_path=log_path,
        symbols=symbols,
        default_bars=default_bars,
    )
