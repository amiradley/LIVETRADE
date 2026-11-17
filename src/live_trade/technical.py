from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from .models import Candle


class TechnicalAnalysisError(ValueError):
    """Raised when OHLC data cannot be analysed safely."""


@dataclass(frozen=True)
class PatternEvidence:
    name: str
    candle_time: datetime
    evidence: Mapping[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "candle_time": self.candle_time.isoformat(),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class StructureObservation:
    direction: str
    labels: tuple[str, ...]
    support: float | None
    resistance: float | None
    breakout: str | None
    false_breakout: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "direction": self.direction,
            "labels": self.labels,
            "support": self.support,
            "resistance": self.resistance,
            "breakout": self.breakout,
            "false_breakout": self.false_breakout,
        }


@dataclass(frozen=True)
class TechnicalAnalysis:
    symbol: str
    timeframe: str
    analysis_time: datetime
    source_candle_time: datetime
    candle_scope: str
    data_quality: str
    indicator_values: Mapping[str, float | None]
    patterns: tuple[PatternEvidence, ...]
    structure: StructureObservation
    technical_score: float | None
    score_breakdown: Mapping[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "analysis_time": self.analysis_time.isoformat(),
            "source_candle_time": self.source_candle_time.isoformat(),
            "candle_scope": self.candle_scope,
            "data_quality": self.data_quality,
            "indicator_values": dict(self.indicator_values),
            "patterns": [pattern.to_dict() for pattern in self.patterns],
            "structure": self.structure.to_dict(),
            "technical_score": self.technical_score,
            "score_breakdown": dict(self.score_breakdown),
        }


@dataclass(frozen=True)
class MultiTimeframeAnalysis:
    symbol: str
    analysis_time: datetime
    direction: str
    agreement: str
    technical_score: float | None
    score_breakdown: Mapping[str, float]
    analyses: Mapping[str, TechnicalAnalysis]

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "analysis_time": self.analysis_time.isoformat(),
            "direction": self.direction,
            "agreement": self.agreement,
            "technical_score": self.technical_score,
            "score_breakdown": dict(self.score_breakdown),
            "timeframes": {timeframe: analysis.to_dict() for timeframe, analysis in self.analyses.items()},
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _finite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise TechnicalAnalysisError(f"Non-finite {label}")
    return value


def _validate_candles(candles: Sequence[Candle]) -> tuple[Candle, ...]:
    if not candles:
        raise TechnicalAnalysisError("No candles available")
    ordered = tuple(candles)
    previous_time: datetime | None = None
    for candle in ordered:
        if previous_time is not None and candle.time <= previous_time:
            raise TechnicalAnalysisError("Candle timestamps must be strictly increasing")
        previous_time = candle.time
        values = {
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }
        for name, value in values.items():
            _finite(float(value), name)
        if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
            raise TechnicalAnalysisError("Candle OHLC relationships are invalid")
        if candle.high < candle.low:
            raise TechnicalAnalysisError("Candle high is below low")
        if candle.tick_volume < 0:
            raise TechnicalAnalysisError("Candle tick volume is negative")
    return ordered


def sma(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def ema(values: Sequence[float], period: int) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    previous = sum(values[:period]) / period
    result[period - 1] = previous
    alpha = 2.0 / (period + 1.0)
    for index in range(period, len(values)):
        previous = (values[index] - previous) * alpha + previous
        result[index] = previous
    return result


def rsi(values: Sequence[float], period: int = 14) -> list[float | None]:
    if period < 1:
        raise ValueError("period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value() -> float:
        if average_loss == 0:
            return 100.0 if average_gain > 0 else 50.0
        relative_strength = average_gain / average_loss
        return 100.0 - (100.0 / (1.0 + relative_strength))

    result[period] = value()
    for index in range(period + 1, len(values)):
        average_gain = ((average_gain * (period - 1)) + gains[index - 1]) / period
        average_loss = ((average_loss * (period - 1)) + losses[index - 1]) / period
        result[index] = value()
    return result


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        if previous_close is None:
            ranges.append(candle.high - candle.low)
        else:
            ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
        previous_close = candle.close
    return ranges


def atr(candles: Sequence[Candle], period: int = 14) -> list[float | None]:
    return sma(true_ranges(candles), period)


def macd(values: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_values = ema(values, fast)
    slow_values = ema(values, slow)
    line: list[float | None] = [
        fast_values[index] - slow_values[index]
        if fast_values[index] is not None and slow_values[index] is not None
        else None
        for index in range(len(values))
    ]
    compact = [value for value in line if value is not None]
    compact_signal = ema(compact, signal)
    signal_line: list[float | None] = [None] * len(values)
    first_valid = next((index for index, value in enumerate(line) if value is not None), len(values))
    for compact_index, value in enumerate(compact_signal):
        index = first_valid + compact_index
        if index < len(signal_line):
            signal_line[index] = value
    histogram = [
        line[index] - signal_line[index]
        if line[index] is not None and signal_line[index] is not None
        else None
        for index in range(len(values))
    ]
    return line, signal_line, histogram


def _last(values: Sequence[float | None]) -> float | None:
    for value in reversed(values):
        if value is not None:
            return value
    return None


def _patterns(candles: Sequence[Candle]) -> tuple[PatternEvidence, ...]:
    current = candles[-1]
    previous = candles[-2] if len(candles) >= 2 else None
    body = abs(current.close - current.open)
    candle_range = current.high - current.low
    if candle_range <= 0:
        return ()
    upper_wick = current.high - max(current.open, current.close)
    lower_wick = min(current.open, current.close) - current.low
    evidence: list[PatternEvidence] = []
    if previous is not None:
        if (
            previous.close < previous.open
            and current.close > current.open
            and current.open <= previous.close
            and current.close >= previous.open
        ):
            evidence.append(PatternEvidence("bullish_engulfing", current.time, {"body_to_range": body / candle_range}))
        if (
            previous.close > previous.open
            and current.close < current.open
            and current.open >= previous.close
            and current.close <= previous.open
        ):
            evidence.append(PatternEvidence("bearish_engulfing", current.time, {"body_to_range": body / candle_range}))
    if body <= candle_range * 0.1:
        evidence.append(PatternEvidence("doji", current.time, {"body_to_range": body / candle_range}))
    if lower_wick >= max(body * 2.0, candle_range * 0.4) and upper_wick <= candle_range * 0.25:
        evidence.append(PatternEvidence("hammer", current.time, {"lower_wick_to_body": lower_wick / max(body, 1e-12)}))
    if upper_wick >= max(body * 2.0, candle_range * 0.4) and lower_wick <= candle_range * 0.25:
        evidence.append(PatternEvidence("shooting_star", current.time, {"upper_wick_to_body": upper_wick / max(body, 1e-12)}))
    if body / candle_range >= 0.6 and current.close > current.open:
        evidence.append(PatternEvidence("strong_bullish", current.time, {"body_to_range": body / candle_range}))
    if body / candle_range >= 0.6 and current.close < current.open:
        evidence.append(PatternEvidence("strong_bearish", current.time, {"body_to_range": body / candle_range}))
    return tuple(evidence)


def _structure(candles: Sequence[Candle], lookback: int = 20) -> StructureObservation:
    window = candles[-min(len(candles), lookback):]
    if len(window) < 4:
        return StructureObservation("NO_CLEAR_DIRECTION", (), None, None, None, None)
    midpoint = max(2, len(window) // 2)
    first = window[:midpoint]
    second = window[midpoint:]
    previous_high = max(candle.high for candle in first)
    recent_high = max(candle.high for candle in second)
    previous_low = min(candle.low for candle in first)
    recent_low = min(candle.low for candle in second)
    labels: list[str] = []
    higher_high = recent_high > previous_high
    higher_low = recent_low > previous_low
    lower_high = recent_high < previous_high
    lower_low = recent_low < previous_low
    if higher_high:
        labels.append("higher_high")
    if higher_low:
        labels.append("higher_low")
    if lower_high:
        labels.append("lower_high")
    if lower_low:
        labels.append("lower_low")
    if higher_high and higher_low:
        direction = "BULLISH"
    elif lower_high and lower_low:
        direction = "BEARISH"
    else:
        direction = "NO_CLEAR_DIRECTION"
    prior = candles[:-1]
    resistance = max(candle.high for candle in prior[-lookback:])
    support = min(candle.low for candle in prior[-lookback:])
    current = candles[-1]
    breakout = None
    false_breakout = None
    if current.close > resistance:
        breakout = "BULLISH"
    elif current.close < support:
        breakout = "BEARISH"
    if current.high > resistance and current.close <= resistance:
        false_breakout = "BULLISH"
    elif current.low < support and current.close >= support:
        false_breakout = "BEARISH"
    return StructureObservation(direction, tuple(labels), support, resistance, breakout, false_breakout)


def _score(
    close: float,
    indicators: Mapping[str, float | None],
    patterns: Sequence[PatternEvidence],
    structure: StructureObservation,
) -> tuple[float, dict[str, float]]:
    ema9 = indicators.get("ema9")
    ema20 = indicators.get("ema20")
    ema50 = indicators.get("ema50")
    if ema9 is not None and ema20 is not None and ema50 is not None and close > ema9 > ema20 > ema50:
        trend = 100.0
    elif ema9 is not None and ema20 is not None and ema50 is not None and close < ema9 < ema20 < ema50:
        trend = 0.0
    else:
        trend = 50.0
    momentum = 50.0
    rsi_value = indicators.get("rsi14")
    macd_line = indicators.get("macd_line")
    macd_signal = indicators.get("macd_signal")
    if rsi_value is not None:
        momentum += 20.0 if rsi_value >= 55 else -20.0 if rsi_value <= 45 else 0.0
    if macd_line is not None and macd_signal is not None:
        momentum += 20.0 if macd_line > macd_signal else -20.0
    momentum = max(0.0, min(100.0, momentum))
    volatility = 70.0 if indicators.get("atr_pct") is not None else 50.0
    bullish_patterns = {"bullish_engulfing", "hammer", "strong_bullish"}
    bearish_patterns = {"bearish_engulfing", "shooting_star", "strong_bearish"}
    pattern_names = {pattern.name for pattern in patterns}
    if pattern_names & bullish_patterns and not pattern_names & bearish_patterns:
        candle_score = 75.0
    elif pattern_names & bearish_patterns and not pattern_names & bullish_patterns:
        candle_score = 25.0
    else:
        candle_score = 50.0
    structure_score = {"BULLISH": 80.0, "BEARISH": 20.0}.get(structure.direction, 50.0)
    weights = {"trend": 0.25, "momentum": 0.20, "volatility": 0.15, "candlestick": 0.15, "structure": 0.25}
    raw = {
        "trend": trend * weights["trend"],
        "momentum": momentum * weights["momentum"],
        "volatility": volatility * weights["volatility"],
        "candlestick": candle_score * weights["candlestick"],
        "structure": structure_score * weights["structure"],
    }
    return round(sum(raw.values()), 4), raw


class TechnicalAnalyzer:
    def __init__(self, minimum_history: int = 50) -> None:
        if minimum_history < 1:
            raise ValueError("minimum_history must be positive")
        self.minimum_history = minimum_history

    def analyze(
        self,
        symbol: str,
        timeframe: str,
        candles: Sequence[Candle],
        analysis_time: datetime | None = None,
        closed_only: bool = True,
    ) -> TechnicalAnalysis:
        timeframe = timeframe.upper()
        validated = _validate_candles(candles)
        if closed_only and len(validated) > 1:
            ordered = validated[:-1]
            candle_scope = "CLOSED"
        else:
            ordered = validated
            candle_scope = "INCLUDING_LIVE"
        closes = [candle.close for candle in ordered]
        sma20 = sma(closes, 20)
        sma50 = sma(closes, 50)
        ema9 = ema(closes, 9)
        ema20 = ema(closes, 20)
        ema50 = ema(closes, 50)
        rsi14 = rsi(closes, 14)
        macd_line, macd_signal, macd_histogram = macd(closes)
        atr14 = atr(ordered, 14)
        current_atr = _last(atr14)
        current_close = closes[-1]
        ranges = [candle.high - candle.low for candle in ordered]
        current_range = ranges[-1]
        previous_volatility = sum(ranges[-6:-1]) / 5 if len(ranges) >= 6 else None
        volatility_change = current_range / previous_volatility - 1 if previous_volatility else None
        indicators: dict[str, float | None] = {
            "sma20": _last(sma20),
            "sma50": _last(sma50),
            "ema9": _last(ema9),
            "ema20": _last(ema20),
            "ema50": _last(ema50),
            "rsi14": _last(rsi14),
            "macd_line": _last(macd_line),
            "macd_signal": _last(macd_signal),
            "macd_histogram": _last(macd_histogram),
            "atr14": current_atr,
            "atr_pct": current_atr / current_close if current_atr is not None and current_close else None,
            "candle_range": current_range,
            "recent_volatility_change": volatility_change,
        }
        patterns = _patterns(ordered)
        structure = _structure(ordered)
        quality = "OK" if len(ordered) >= self.minimum_history else "INSUFFICIENT_HISTORY"
        score, breakdown = _score(current_close, indicators, patterns, structure)
        if quality != "OK":
            score = None
            breakdown = {}
        return TechnicalAnalysis(
            symbol=symbol,
            timeframe=timeframe,
            analysis_time=_utc(analysis_time or datetime.now(timezone.utc)),
            source_candle_time=_utc(ordered[-1].time),
            candle_scope=candle_scope,
            data_quality=quality,
            indicator_values=indicators,
            patterns=patterns,
            structure=structure,
            technical_score=score,
            score_breakdown=breakdown,
        )


def analyze_multi_timeframe(
    symbol: str,
    candles_by_timeframe: Mapping[str, Sequence[Candle]],
    analyzer: TechnicalAnalyzer | None = None,
    analysis_time: datetime | None = None,
    closed_only: bool = True,
) -> MultiTimeframeAnalysis:
    analyzer = analyzer or TechnicalAnalyzer()
    analyses: dict[str, TechnicalAnalysis] = {}
    for timeframe, candles in candles_by_timeframe.items():
        try:
            analyses[timeframe.upper()] = analyzer.analyze(
                symbol, timeframe, candles, analysis_time, closed_only=closed_only
            )
        except TechnicalAnalysisError:
            continue
    directions = [analysis.structure.direction for analysis in analyses.values() if analysis.data_quality == "OK"]
    bullish = sum(direction == "BULLISH" for direction in directions)
    bearish = sum(direction == "BEARISH" for direction in directions)
    if bullish and bearish:
        direction, agreement = "NO_CLEAR_DIRECTION", "CONFLICTING"
    elif bullish:
        direction, agreement = "BULLISH", "ALIGNED" if bullish == len(directions) else "MIXED"
    elif bearish:
        direction, agreement = "BEARISH", "ALIGNED" if bearish == len(directions) else "MIXED"
    else:
        direction, agreement = "NO_CLEAR_DIRECTION", "INSUFFICIENT_DATA"
    scores = [analysis.technical_score for analysis in analyses.values() if analysis.technical_score is not None]
    score = round(sum(scores) / len(scores), 4) if scores else None
    return MultiTimeframeAnalysis(
        symbol=symbol,
        analysis_time=_utc(analysis_time or datetime.now(timezone.utc)),
        direction=direction,
        agreement=agreement,
        technical_score=score,
        score_breakdown={"average_timeframe_score": score or 0.0, "aligned_timeframes": float(bullish + bearish)},
        analyses=analyses,
    )
