from datetime import datetime, timedelta, timezone

import pytest

from live_trade.models import Candle
from live_trade.technical import (
    TechnicalAnalysisError,
    TechnicalAnalyzer,
    analyze_multi_timeframe,
    atr,
    ema,
    macd,
    rsi,
    sma,
)


def make_candles(closes, opens=None):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    opens = opens or [close - 0.01 for close in closes]
    return tuple(
        Candle(
            symbol="EURUSD",
            timeframe="M1",
            time=start + timedelta(minutes=index),
            open=open_price,
            high=max(open_price, close) + 0.02,
            low=min(open_price, close) - 0.02,
            close=close,
            tick_volume=100,
        )
        for index, (open_price, close) in enumerate(zip(opens, closes))
    )


def test_indicator_warmup_and_known_values():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sma(values, 3)[:2] == [None, None]
    assert sma(values, 3)[-1] == pytest.approx(4.0)
    assert ema(values, 3)[-1] == pytest.approx(4.0)
    assert rsi(values, 3)[-1] == pytest.approx(100.0)
    candles = make_candles(values)
    assert atr(candles, 3)[-1] is not None
    line, signal, histogram = macd(values, fast=2, slow=3, signal=2)
    assert len(line) == len(signal) == len(histogram) == len(values)


def test_invalid_ohlc_is_rejected():
    candles = list(make_candles([1.0, 1.1]))
    candles[1] = Candle(
        symbol="EURUSD",
        timeframe="M1",
        time=candles[1].time,
        open=1.1,
        high=1.0,
        low=0.9,
        close=1.1,
        tick_volume=1,
    )
    with pytest.raises(TechnicalAnalysisError, match="OHLC relationships"):
        TechnicalAnalyzer().analyze("EURUSD", "M1", candles)


def test_patterns_include_numerical_evidence():
    candles = list(make_candles([1.10, 1.08], opens=[1.05, 1.11]))
    candles[-1] = Candle(
        symbol="EURUSD",
        timeframe="M1",
        time=candles[-1].time,
        open=1.08,
        high=1.09,
        low=1.00,
        close=1.09,
        tick_volume=100,
    )
    analysis = TechnicalAnalyzer(minimum_history=2).analyze(
        "EURUSD", "M1", candles, closed_only=False
    )
    names = {pattern.name for pattern in analysis.patterns}
    assert "hammer" in names
    assert all(pattern.evidence for pattern in analysis.patterns)


def test_confirmed_analysis_excludes_current_forming_candle():
    candles = make_candles([1.0, 1.01, 1.02])
    analysis = TechnicalAnalyzer(minimum_history=2).analyze("EURUSD", "M1", candles)

    assert analysis.candle_scope == "CLOSED"
    assert analysis.source_candle_time == candles[-2].time


def test_structure_and_multi_timeframe_conflict_are_explicit():
    bullish = make_candles([1.00 + index * 0.01 for index in range(60)])
    bearish = make_candles([2.00 - index * 0.01 for index in range(60)])
    analyzer = TechnicalAnalyzer(minimum_history=50)
    bullish_analysis = analyzer.analyze("EURUSD", "H1", bullish)
    bearish_analysis = analyzer.analyze("EURUSD", "M1", bearish)
    combined = analyze_multi_timeframe(
        "EURUSD",
        {"H1": bullish, "M1": bearish},
        analyzer,
    )

    assert bullish_analysis.structure.direction == "BULLISH"
    assert bearish_analysis.structure.direction == "BEARISH"
    assert combined.direction == "NO_CLEAR_DIRECTION"
    assert combined.agreement == "CONFLICTING"


def test_technical_score_has_measurable_breakdown():
    candles = make_candles([1.00 + index * 0.01 for index in range(60)])
    analysis = TechnicalAnalyzer().analyze("EURUSD", "M1", candles)

    assert analysis.data_quality == "OK"
    assert analysis.technical_score is not None
    assert 0 <= analysis.technical_score <= 100
    assert set(analysis.score_breakdown) == {
        "trend",
        "momentum",
        "volatility",
        "candlestick",
        "structure",
    }
