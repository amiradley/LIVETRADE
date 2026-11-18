from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Mapping


_FOREX = re.compile(r"^[A-Z]{6}([._-].*)?$")


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def instrument_category(raw: Mapping[str, Any]) -> str:
    text = " ".join(str(raw.get(key) or "") for key in ("category", "path", "description", "exchange", "name")).lower()
    name = str(raw.get("name") or "").upper()
    if "forex" in text or _FOREX.match(name):
        return "FOREX"
    if any(token in text for token in ("metal", "gold", "silver")) or name.startswith(("XAU", "XAG")):
        return "METALS"
    if any(token in text for token in ("energy", "oil", "gas", "brent", "wti")):
        return "ENERGY"
    if any(token in text for token in ("index", "indices")):
        return "INDICES"
    if any(token in text for token in ("crypto", "bitcoin", "ethereum")):
        return "CRYPTOCURRENCY"
    if any(token in text for token in ("etf", "exchange traded")):
        return "ETF"
    if any(token in text for token in ("stock", "share", "equity")):
        return "STOCKS"
    if any(token in text for token in ("future", "futures")):
        return "FUTURES"
    if "cfd" in text:
        return "CFD"
    return "OTHER"


def classify_instrument(raw: Mapping[str, Any], refreshed_at: datetime | None = None) -> dict[str, Any]:
    now = (refreshed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    symbol = str(raw.get("name") or raw.get("symbol") or "").strip().upper()
    category = instrument_category(raw)
    point = _number(raw.get("point"))
    contract_size = _number(raw.get("trade_contract_size"))
    volume_min = _number(raw.get("volume_min"))
    volume_step = _number(raw.get("volume_step"))
    bid = _number(raw.get("bid"))
    ask = _number(raw.get("ask"))
    spread = _number(raw.get("spread"))
    reasons: list[str] = []
    if not symbol:
        reasons.append("missing_symbol")
    if point is None or point <= 0:
        reasons.append("invalid_tick_size")
    if contract_size is None or contract_size <= 0:
        reasons.append("invalid_contract_size")
    if volume_min is None or volume_min <= 0 or volume_step is None or volume_step <= 0:
        reasons.append("invalid_volume_specification")
    trade_mode = int(raw.get("trade_mode") or 0)
    trading_status = "DISABLED" if trade_mode == 0 else "ENABLED_OR_RESTRICTED"
    if trade_mode == 0:
        reasons.append("trading_disabled_or_restricted")
    market_state = "CURRENT_QUOTE" if (bid is not None and ask is not None and (bid > 0 or ask > 0)) else "NO_CURRENT_QUOTE"
    if market_state == "NO_CURRENT_QUOTE":
        reasons.append("market_closed_or_quote_unavailable")
    if reasons and set(reasons).issubset({"trading_disabled_or_restricted", "market_closed_or_quote_unavailable"}):
        suitability = "ELIGIBLE_WITH_CAUTION"
    elif any(reason.startswith("invalid_") or reason == "missing_symbol" for reason in reasons):
        suitability = "INSUFFICIENT_DATA"
    elif category in {"FOREX", "METALS", "ENERGY", "INDICES", "FUTURES", "CRYPTOCURRENCY"} and trade_mode != 0:
        suitability = "ELIGIBLE"
    elif category in {"STOCKS", "ETF", "CFD"}:
        suitability = "ELIGIBLE_WITH_CAUTION"
    else:
        suitability = "LOW_PRIORITY"
    return {
        "symbol": symbol,
        "refreshed_at": now.isoformat(),
        "category": category,
        "description": str(raw.get("description") or ""),
        "path": str(raw.get("path") or ""),
        "exchange": str(raw.get("exchange") or ""),
        "currency_base": raw.get("currency_base"),
        "currency_profit": raw.get("currency_profit"),
        "currency_margin": raw.get("currency_margin"),
        "contract_size": contract_size,
        "tick_size": _number(raw.get("trade_tick_size")) or point,
        "tick_value": _number(raw.get("trade_tick_value")),
        "volume_min": volume_min,
        "volume_max": _number(raw.get("volume_max")),
        "volume_step": volume_step,
        "trade_mode": trade_mode,
        "visible": raw.get("visible"),
        "bid": bid,
        "ask": ask,
        "spread_points": spread,
        "market_state": market_state,
        "trading_status": trading_status,
        "suitability": suitability,
        "suitability_reasons": reasons,
        "raw": dict(raw),
    }


def priority_for(classification: Mapping[str, Any]) -> float:
    suitability = classification.get("suitability")
    category = classification.get("category")
    score = {"ELIGIBLE": 100.0, "ELIGIBLE_WITH_CAUTION": 60.0, "LOW_PRIORITY": 20.0, "INSUFFICIENT_DATA": 0.0}.get(str(suitability), 0.0)
    score += {"FOREX": 20.0, "METALS": 16.0, "ENERGY": 14.0, "INDICES": 12.0, "FUTURES": 8.0, "CRYPTOCURRENCY": 6.0}.get(str(category), 0.0)
    spread = _number(classification.get("spread_points"))
    if spread is not None:
        score -= min(spread, 20.0)
    return max(0.0, score)
