from datetime import datetime, timedelta, timezone

from live_trade.models import Candle
from live_trade.research_intelligence import BacktestConfig, BacktestEngine, ResearchService, generate_hypotheses
from live_trade.storage import MarketDataStore


def candles(count=140):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return tuple(
        Candle(
            "EURUSD", "M1", start + timedelta(minutes=index),
            1.1000 + index * 0.00001,
            1.1005 + index * 0.00001,
            1.0995 + index * 0.00001,
            1.1002 + index * 0.00001,
            100,
        )
        for index in range(count)
    )


def test_hypotheses_are_reproducible_and_explicit():
    first = generate_hypotheses("EURUSD", "M1")
    second = generate_hypotheses("EURUSD", "M1")

    assert [item.hypothesis_id for item in first] == [item.hypothesis_id for item in second]
    assert {item.family for item in first} == {"MOMENTUM", "MEAN_REVERSION", "BREAKOUT", "VOLATILITY"}
    assert all(item.definition["signal_timing"] == "signal_on_closed_candle_entry_next_open" for item in first)


def test_backtest_records_cost_assumptions_and_metrics():
    hypothesis = generate_hypotheses("EURUSD", "M1")[0]
    result = BacktestEngine().run(hypothesis, candles(), BacktestConfig(spread_points=25, slippage_points=2))

    assert result.assumptions["lookahead_protection"] is True
    assert result.assumptions["entry"] == "next_candle_open"
    assert "sample_size" in result.metrics
    assert "max_drawdown_percent" in result.metrics
    assert result.metrics["candle_count"] == 140


def test_research_service_persists_experiments_results_and_learning(tmp_path):
    store = MarketDataStore(tmp_path / "market.sqlite3")
    collected = datetime.now(timezone.utc).isoformat()
    records = [
        {
            "symbol": candle.symbol, "timeframe": candle.timeframe, "raw_time": candle.time.isoformat(),
            "effective_time": candle.time.isoformat(), "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "tick_volume": candle.tick_volume, "spread": candle.spread, "real_volume": candle.real_volume,
            "quality_status": "VALID", "quality_reasons": [], "collected_at": collected,
        }
        for candle in candles()
    ]
    store.save_historical_candles(records)
    service = ResearchService(store)

    first = service.run("EURUSD", "M1", limit=4)
    second = service.run("EURUSD", "M1", limit=4)
    summary = store.research_summary()

    assert len(first["processed"]) == 4
    assert second["processed"] == []
    assert summary["hypotheses"] == 4
    assert summary["experiments"] == 4
    assert summary["validation_runs"] == 16
    assert summary["learning_observations"] == 4
