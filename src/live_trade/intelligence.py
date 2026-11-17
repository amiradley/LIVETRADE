from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from .accounting import HistoricalTrade, aggregate_closed_trades, performance_summary


@dataclass(frozen=True)
class ExchangeRate:
    base_currency: str
    quote_currency: str
    rate: float | None
    source: str | None
    retrieved_at: datetime | None
    quote_date: date | None
    status: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "rate": self.rate,
            "source": self.source,
            "retrieved_at": self.retrieved_at.isoformat() if self.retrieved_at else None,
            "quote_date": self.quote_date.isoformat() if self.quote_date else None,
            "status": self.status,
            "error": self.error,
        }


class ExchangeRateService:
    """Fetch and validate FX rates; never invents a fallback rate."""

    def __init__(self, store: Any, cache_seconds: int = 900, max_quote_age_days: int = 2) -> None:
        self.store = store
        self.cache_seconds = cache_seconds
        self.max_quote_age_days = max_quote_age_days
        self._cache: dict[tuple[str, str], tuple[float, ExchangeRate]] = {}

    def get_rate(self, base_currency: str, quote_currency: str = "MYR") -> ExchangeRate:
        base = base_currency.upper()
        quote = quote_currency.upper()
        if base == quote:
            return ExchangeRate(base, quote, 1.0, "identity", datetime.now(timezone.utc), date.today(), "INTERNALLY_CALCULATED")
        key = (base, quote)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]
        url_template = os.getenv(
            "LIVE_TRADE_FX_URL",
            "https://api.frankfurter.app/latest?from={base}&to={quote}",
        )
        url = url_template.format(base=urllib.parse.quote(base), quote=urllib.parse.quote(quote))
        retrieved_at = datetime.now(timezone.utc)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "LIVE-TRADING/1.6"})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_rate = payload.get("rates", {}).get(quote)
            rate = float(raw_rate)
            if rate <= 0:
                raise ValueError("FX rate must be positive")
            quote_date = date.fromisoformat(str(payload.get("date")))
            status = "OK" if (retrieved_at.date() - quote_date).days <= self.max_quote_age_days else "STALE"
            result = ExchangeRate(base, quote, rate, url, retrieved_at, quote_date, status)
            self._cache[key] = (now, result)
            self.store.save_exchange_rate(result)
            return result
        except Exception as exc:
            if cached and cached[1].rate is not None:
                previous = cached[1]
                return ExchangeRate(base, quote, previous.rate, previous.source, previous.retrieved_at, previous.quote_date, "STALE", str(exc))
            return ExchangeRate(base, quote, None, url, retrieved_at, None, "UNAVAILABLE", str(exc))


@dataclass(frozen=True)
class FinancialIntelligenceSnapshot:
    history: tuple[HistoricalTrade, ...]
    raw_deal_count: int
    history_status: str
    history_error: str | None
    performance: Mapping[str, float | None]
    exchange_rate: ExchangeRate
    allocation: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "history": [trade.to_dict() for trade in self.history],
            "raw_deal_count": self.raw_deal_count,
            "history_status": self.history_status,
            "history_error": self.history_error,
            "performance": dict(self.performance),
            "exchange_rate": self.exchange_rate.to_dict(),
            "allocation": dict(self.allocation),
        }


class FinancialIntelligenceService:
    def __init__(self, store: Any, lookback_days: int = 365) -> None:
        self.store = store
        self.lookback_days = lookback_days
        self.exchange_rates = ExchangeRateService(store)

    def capture(self, client: Any, account_currency: str, captured_at: datetime | None = None) -> FinancialIntelligenceSnapshot:
        captured_at = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        history_error = None
        try:
            raw_deals = client.history_deals(captured_at - timedelta(days=self.lookback_days), captured_at)
            self.store.save_broker_deals(raw_deals, captured_at)
            history_status = "BROKER_VERIFIED"
        except Exception as exc:
            raw_deals = ()
            history_status = "UNAVAILABLE"
            history_error = str(exc)
        history = aggregate_closed_trades(raw_deals)
        performance = performance_summary(history, captured_at)
        allocation = self.store.apply_savings_allocation(
            float(performance["cumulative"] or 0.0), len(raw_deals), captured_at
        )
        exchange_rate = self.exchange_rates.get_rate(account_currency, "MYR")
        return FinancialIntelligenceSnapshot(
            history=history,
            raw_deal_count=len(raw_deals),
            history_status=history_status,
            history_error=history_error,
            performance=performance,
            exchange_rate=exchange_rate,
            allocation=allocation,
        )
