from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .models import Candle, Tick
from .technical import TechnicalAnalysis


DECISION_LABEL = "EXPERIMENTAL — UNVALIDATED"


@dataclass(frozen=True)
class TradeDecision:
    signal_id: str
    decision: str
    symbol: str
    timeframe: str
    analysis_time: datetime
    source_candle_time: datetime
    label: str
    market_conditions: dict[str, Any]
    rationale: str
    entry: float | None
    stop_loss: float | None
    take_profit: float | None
    risk_reward: float | None
    invalidation: tuple[str, ...]
    confidence: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "decision": self.decision,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "analysis_time": self.analysis_time.isoformat(),
            "source_candle_time": self.source_candle_time.isoformat(),
            "label": self.label,
            "market_conditions": self.market_conditions,
            "rationale": self.rationale,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_reward": self.risk_reward,
            "invalidation": list(self.invalidation),
            "confidence": self.confidence,
        }


def build_experimental_decision(
    analysis: TechnicalAnalysis,
    tick: Tick,
    candles: tuple[Candle, ...] | list[Candle],
) -> TradeDecision:
    analysis_time = analysis.analysis_time.astimezone(timezone.utc)
    source_time = analysis.source_candle_time.astimezone(timezone.utc)
    signal_seed = f"{analysis.symbol}|{analysis.timeframe}|{source_time.isoformat()}|{analysis.technical_score}"
    signal_id = f"signal_{hashlib.sha256(signal_seed.encode('utf-8')).hexdigest()[:24]}"
    indicators = dict(analysis.indicator_values)
    structure = analysis.structure
    score = analysis.technical_score
    atr_value = indicators.get("atr14")
    market_conditions = {
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": tick.ask - tick.bid,
        "trend": structure.direction,
        "momentum": indicators.get("rsi14"),
        "volatility_atr": atr_value,
        "technical_score": score,
        "candle_count": len(candles),
        "data_quality": analysis.data_quality,
        "structure": structure.to_dict(),
    }
    decision = "NO_TRADE"
    entry = stop_loss = take_profit = risk_reward = None
    reasons: list[str] = []
    if analysis.data_quality != "OK":
        reasons.append("insufficient closed-candle history")
    elif score is None or atr_value is None or atr_value <= 0:
        reasons.append("indicators are unavailable or invalid")
    elif structure.direction == "BULLISH" and score >= 70:
        decision = "BUY"
        entry = tick.ask
        stop_loss = entry - 2.0 * atr_value
        take_profit = entry + 4.0 * atr_value
        risk_reward = 2.0
        reasons.append("bullish structure and technical score meet the exploratory threshold")
    elif structure.direction == "BEARISH" and score <= 30:
        decision = "SELL"
        entry = tick.bid
        stop_loss = entry + 2.0 * atr_value
        take_profit = entry - 4.0 * atr_value
        risk_reward = 2.0
        reasons.append("bearish structure and technical score meet the exploratory threshold")
    else:
        reasons.append("trend and score do not form a sufficiently aligned exploratory setup")
    invalidation = (
        "A new closed candle invalidates the signal after its expiry.",
        "A spread or data-freshness protection failure invalidates the signal.",
        "A broker or risk-boundary check failure prevents execution.",
    )
    confidence = round(abs(score - 50.0) * 2.0, 2) if score is not None else None
    return TradeDecision(
        signal_id=signal_id,
        decision=decision,
        symbol=analysis.symbol,
        timeframe=analysis.timeframe,
        analysis_time=analysis_time,
        source_candle_time=source_time,
        label=DECISION_LABEL,
        market_conditions=market_conditions,
        rationale="; ".join(reasons),
        entry=entry,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward=risk_reward,
        invalidation=invalidation,
        confidence=confidence,
    )
