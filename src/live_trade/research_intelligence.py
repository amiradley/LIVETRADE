from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from .models import Candle
from .storage import MarketDataStore


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    name: str
    family: str
    rationale: str
    definition: dict[str, Any]
    version: str = "1.0"
    status: str = "EXPERIMENTAL"
    falsification_criteria: tuple[str, ...] = (
        "Out-of-sample expectancy is non-positive after costs",
        "Results depend on one exact parameter value",
        "Fewer than 30 independent trades are available",
    )

    def to_payload(self, created_at: datetime | None = None) -> dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "name": self.name,
            "family": self.family,
            "rationale": self.rationale,
            "definition_json": json.dumps(self.definition, sort_keys=True),
            "version": self.version,
            "status": self.status,
            "falsification_json": json.dumps(self.falsification_criteria),
            "created_at": (created_at or _utc_now()).isoformat(),
        }


def generate_hypotheses(symbol: str, timeframe: str) -> tuple[Hypothesis, ...]:
    definitions = (
        ("MOMENTUM", "Trend continuation", "A sufficiently persistent directional move may continue after a confirmed moving-average alignment.", {"lookback": 20, "holding_bars": 8, "stop_atr": 1.5, "take_profit_atr": 2.0}),
        ("MEAN_REVERSION", "Volatility-adjusted mean reversion", "Extreme deviation from a recent mean may revert when volatility is not expanding indefinitely.", {"lookback": 20, "deviation": 2.0, "holding_bars": 8, "stop_atr": 1.5, "take_profit_atr": 1.0}),
        ("BREAKOUT", "Range breakout", "A close beyond a recent range may precede continuation, subject to explicit cost and stop assumptions.", {"lookback": 20, "holding_bars": 8, "stop_atr": 1.5, "take_profit_atr": 2.0}),
        ("VOLATILITY", "Volatility expansion direction", "A range expansion may carry directional information, but must survive spread and slippage stress.", {"lookback": 14, "holding_bars": 6, "stop_atr": 1.0, "take_profit_atr": 1.5}),
    )
    result = []
    for family, name, rationale, parameters in definitions:
        definition = {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "parameters": parameters, "signal_timing": "signal_on_closed_candle_entry_next_open"}
        hypothesis_id = _stable_id("hyp", {"family": family, "definition": definition})
        result.append(Hypothesis(hypothesis_id, name, family, rationale, definition))
    return tuple(result)


def _sma(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _std(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return statistics.pstdev(values[-period:])


def _atr(candles: Sequence[Candle], period: int) -> float | None:
    if len(candles) < period + 1:
        return None
    values = []
    for previous, candle in zip(candles[-period - 1 : -1], candles[-period:]):
        values.append(max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)))
    return sum(values) / len(values)


@dataclass(frozen=True)
class BacktestConfig:
    initial_equity: float = 10_000.0
    risk_per_trade: float = 0.005
    point: float = 0.00001
    spread_points: float = 20.0
    slippage_points: float = 1.0
    commission_per_unit: float = 0.0
    max_units: float = 1_000_000.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class BacktestResult:
    metrics: dict[str, Any]
    trades: tuple[dict[str, Any], ...]
    assumptions: dict[str, Any]


class BacktestEngine:
    """Conservative OHLC-only research simulator; it has no broker execution path."""

    def _signal(self, family: str, candles: Sequence[Candle], index: int, parameters: dict[str, Any]) -> int:
        lookback = int(parameters.get("lookback", 20))
        history = candles[:index]
        closes = [candle.close for candle in history]
        if len(closes) < lookback:
            return 0
        current = closes[-1]
        mean = _sma(closes, lookback)
        if mean is None:
            return 0
        if family == "MOMENTUM":
            previous_mean = _sma(closes[:-1], lookback) if len(closes) > lookback else None
            if previous_mean is not None and current > mean and current > previous_mean:
                return 1
            if previous_mean is not None and current < mean and current < previous_mean:
                return -1
        elif family == "MEAN_REVERSION":
            deviation = float(parameters.get("deviation", 2.0)) * (_std(closes, lookback) or 0.0)
            if deviation > 0 and current < mean - deviation:
                return 1
            if deviation > 0 and current > mean + deviation:
                return -1
        elif family == "BREAKOUT":
            prior = history[-lookback:]
            if current > max(candle.high for candle in prior[:-1]):
                return 1
            if current < min(candle.low for candle in prior[:-1]):
                return -1
        elif family == "VOLATILITY":
            current_range = history[-1].high - history[-1].low
            average_range = sum(candle.high - candle.low for candle in history[-lookback:]) / lookback
            if current_range > average_range * 1.5:
                return 1 if history[-1].close >= history[-1].open else -1
        return 0

    def run(self, hypothesis: Hypothesis, candles: Sequence[Candle], config: BacktestConfig, label: str = "FULL") -> BacktestResult:
        if len(candles) < max(30, int(hypothesis.definition["parameters"].get("lookback", 20)) + 5):
            return BacktestResult(self._metrics([], config, label, len(candles), "INSUFFICIENT_DATA"), (), {"config": config.to_dict(), "label": label, "lookahead_protection": True})
        parameters = hypothesis.definition["parameters"]
        lookback = int(parameters.get("lookback", 20))
        holding_bars = int(parameters.get("holding_bars", 8))
        trades: list[dict[str, Any]] = []
        i = lookback + 1
        while i < len(candles) - 1:
            signal = self._signal(hypothesis.family, candles, i, parameters)
            if signal == 0:
                i += 1
                continue
            entry_candle = candles[i]
            atr = _atr(candles[:i], min(14, lookback)) or max(entry_candle.high - entry_candle.low, config.point * 10)
            stop_distance = max(atr * float(parameters.get("stop_atr", 1.5)), config.point)
            target_distance = stop_distance * float(parameters.get("take_profit_atr", 2.0)) / max(float(parameters.get("stop_atr", 1.5)), 0.1)
            raw_entry = entry_candle.open
            spread = config.spread_points * config.point
            entry = raw_entry + signal * (spread / 2 + config.slippage_points * config.point)
            stop = entry - signal * stop_distance
            target = entry + signal * target_distance
            units = min(config.max_units, config.initial_equity * config.risk_per_trade / stop_distance)
            exit_index = min(len(candles) - 1, i + holding_bars)
            exit_price = candles[exit_index].close
            exit_reason = "TIME"
            for j in range(i, exit_index + 1):
                bar = candles[j]
                if signal == 1 and bar.low <= stop:
                    exit_price, exit_index, exit_reason = stop - spread / 2 - config.slippage_points * config.point, j, "STOP"
                    break
                if signal == 1 and bar.high >= target:
                    exit_price, exit_index, exit_reason = target - spread / 2 - config.slippage_points * config.point, j, "TARGET"
                    break
                if signal == -1 and bar.high >= stop:
                    exit_price, exit_index, exit_reason = stop + spread / 2 + config.slippage_points * config.point, j, "STOP"
                    break
                if signal == -1 and bar.low <= target:
                    exit_price, exit_index, exit_reason = target + spread / 2 + config.slippage_points * config.point, j, "TARGET"
                    break
            pnl = (exit_price - entry) * units * signal - config.commission_per_unit * units
            trades.append({"entry_time": entry_candle.time.isoformat(), "exit_time": candles[exit_index].time.isoformat(), "direction": "LONG" if signal == 1 else "SHORT", "entry": entry, "exit": exit_price, "units": units, "pnl": pnl, "r_multiple": pnl / max(config.initial_equity * config.risk_per_trade, 1e-12), "holding_bars": exit_index - i + 1, "exit_reason": exit_reason})
            i = exit_index + 1
        return BacktestResult(self._metrics(trades, config, label, len(candles), "OK"), tuple(trades), {"config": config.to_dict(), "label": label, "lookahead_protection": True, "entry": "next_candle_open", "ohlc_limitations": "Bid/ask history unavailable; configured spread/slippage model applied."})

    def _metrics(self, trades: Sequence[dict[str, Any]], config: BacktestConfig, label: str, candle_count: int, data_status: str) -> dict[str, Any]:
        pnls = [float(trade["pnl"]) for trade in trades]
        wins = [pnl for pnl in pnls if pnl > 0]
        losses = [pnl for pnl in pnls if pnl < 0]
        equity = config.initial_equity
        peak = equity
        max_drawdown = 0.0
        for pnl in pnls:
            equity += pnl
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
        returns = [pnl / config.initial_equity for pnl in pnls]
        mean_return = statistics.mean(returns) if returns else 0.0
        std_return = statistics.stdev(returns) if len(returns) > 1 else 0.0
        downside = [value for value in returns if value < 0]
        downside_std = statistics.pstdev(downside) if len(downside) > 1 else 0.0
        expectancy = statistics.mean(pnls) if pnls else 0.0
        se = (statistics.stdev(pnls) / math.sqrt(len(pnls))) if len(pnls) > 1 else None
        drawdown_duration = 0
        current_drawdown_duration = 0
        running_equity = config.initial_equity
        running_peak = running_equity
        for pnl in pnls:
            running_equity += pnl
            running_peak = max(running_peak, running_equity)
            current_drawdown_duration = current_drawdown_duration + 1 if running_equity < running_peak else 0
            drawdown_duration = max(drawdown_duration, current_drawdown_duration)
        risk_of_ruin = None if len(pnls) < 30 else (1.0 if expectancy <= 0 or not wins else max(0.0, min(1.0, (len(losses) / len(pnls)) ** 10)))
        return {"label": label, "data_status": data_status, "candle_count": candle_count, "sample_size": len(trades), "net_profit": sum(pnls), "gross_profit": sum(wins), "gross_loss": sum(losses), "return": (equity - config.initial_equity) / config.initial_equity, "average_trade_return": mean_return, "average_win": statistics.mean(wins) if wins else 0.0, "average_loss": statistics.mean(losses) if losses else 0.0, "median_trade": statistics.median(pnls) if pnls else 0.0, "average_holding_bars": statistics.mean([trade["holding_bars"] for trade in trades]) if trades else 0.0, "expectancy": expectancy, "expectancy_r": statistics.mean([trade["r_multiple"] for trade in trades]) if trades else 0.0, "profit_factor": (sum(wins) / abs(sum(losses))) if losses else None, "max_drawdown": max_drawdown, "max_drawdown_percent": max_drawdown / config.initial_equity, "max_drawdown_duration_trades": drawdown_duration, "win_rate": len(wins) / len(pnls) if pnls else 0.0, "consecutive_losses": self._consecutive_losses(pnls), "tail_loss_worst_5_percent": min(pnls) if pnls else 0.0, "risk_of_ruin_estimate": risk_of_ruin, "trade_frequency": len(pnls) / candle_count if candle_count else 0.0, "sharpe_trade_level": (mean_return / std_return * math.sqrt(len(returns))) if std_return else None, "sortino_trade_level": (mean_return / downside_std * math.sqrt(len(returns))) if downside_std else None, "expectancy_95ci": [expectancy - 1.96 * se, expectancy + 1.96 * se] if se is not None else None, "final_equity": equity}

    @staticmethod
    def _consecutive_losses(pnls: Sequence[float]) -> int:
        current = longest = 0
        for pnl in pnls:
            current = current + 1 if pnl < 0 else 0
            longest = max(longest, current)
        return longest


class ResearchService:
    def __init__(self, store: MarketDataStore) -> None:
        self.store = store
        self.engine = BacktestEngine()

    def queue_hypotheses(self, symbol: str, timeframe: str, limit: int = 4) -> list[str]:
        now = _utc_now()
        hypothesis_ids = []
        candles = self.store.load_historical_candles(symbol, timeframe)
        data_reference = {"table": "historical_candles", "symbol": symbol.upper(), "timeframe": timeframe.upper(), "candle_count": len(candles), "quality_boundary": "readiness_audited_separately"}
        for hypothesis in generate_hypotheses(symbol, timeframe)[:limit]:
            self.store.research_save("hypothesis", hypothesis.to_payload(now))
            config = BacktestConfig().to_dict()
            experiment_id = _stable_id("exp", {"hypothesis_id": hypothesis.hypothesis_id, "symbol": symbol.upper(), "timeframe": timeframe.upper(), "config": config, "data": data_reference})
            self.store.research_save("experiment", {"experiment_id": experiment_id, "hypothesis_id": hypothesis.hypothesis_id, "symbol": symbol.upper(), "timeframe": timeframe.upper(), "data_start": candles[0].time.isoformat() if candles else None, "data_end": candles[-1].time.isoformat() if candles else None, "config_json": json.dumps(config, sort_keys=True), "data_reference_json": json.dumps(data_reference, sort_keys=True), "priority": 100.0 if candles else 0.0, "status": "QUEUED", "progress": 0.0, "failure_reason": None, "created_at": now.isoformat(), "updated_at": now.isoformat()})
            hypothesis_ids.append(hypothesis.hypothesis_id)
        return hypothesis_ids

    def run(self, symbol: str, timeframe: str, limit: int = 4, force: bool = False) -> dict[str, Any]:
        self.queue_hypotheses(symbol, timeframe, limit)
        rows = self.store.research_rows("research_experiments", 100)
        processed = []
        for row in rows:
            if row["symbol"] != symbol.upper() or row["timeframe"] != timeframe.upper() or (row["status"] == "COMPLETE" and not force):
                continue
            self.store.research_update_experiment(row["experiment_id"], "RUNNING", 0.1)
            hypothesis_row = next(item for item in self.store.research_rows("research_hypotheses", 100) if item["hypothesis_id"] == row["hypothesis_id"])
            definition = json.loads(hypothesis_row["definition_json"])
            hypothesis = Hypothesis(hypothesis_row["hypothesis_id"], hypothesis_row["name"], hypothesis_row["family"], hypothesis_row["rationale"], definition, hypothesis_row["version"], hypothesis_row["status"], tuple(json.loads(hypothesis_row["falsification_json"])))
            candles = self.store.load_historical_candles(symbol, timeframe)
            if len(candles) < 100:
                self.store.research_update_experiment(row["experiment_id"], "COMPLETE", 1.0, "Insufficient historical sample")
                self.store.research_save("validation", {"experiment_id": row["experiment_id"], "stage": "INITIAL_SCREEN", "status": "INCONCLUSIVE", "metrics_json": json.dumps({"candle_count": len(candles)}), "failure_reasons_json": json.dumps(["insufficient_historical_data"]), "created_at": _utc_now().isoformat()})
                processed.append({"experiment_id": row["experiment_id"], "status": "INCONCLUSIVE", "reason": "insufficient_historical_data"})
                continue
            split = max(1, int(len(candles) * 0.7))
            train = candles[:split]
            test = candles[split:]
            config = BacktestConfig(**json.loads(row["config_json"]))
            train_result = self.engine.run(hypothesis, train, config, "TRAIN")
            test_result = self.engine.run(hypothesis, test, config, "OUT_OF_SAMPLE")
            run_key = _stable_id("run", {"experiment_id": row["experiment_id"], "config": config.to_dict()})
            self.store.research_save("backtest", {"experiment_id": row["experiment_id"], "run_key": run_key, "metrics_json": json.dumps({"train": train_result.metrics, "out_of_sample": test_result.metrics}), "trades_json": json.dumps({"train": train_result.trades, "out_of_sample": test_result.trades}, default=str), "assumptions_json": json.dumps({"train": train_result.assumptions, "out_of_sample": test_result.assumptions}, default=str), "created_at": _utc_now().isoformat()})
            validation_status, failures = self._validate(train_result, test_result)
            self.store.research_save("validation", {"experiment_id": row["experiment_id"], "stage": "INITIAL_AND_OUT_OF_SAMPLE", "status": validation_status, "metrics_json": json.dumps({"train": train_result.metrics, "out_of_sample": test_result.metrics}), "failure_reasons_json": json.dumps(failures), "created_at": _utc_now().isoformat()})
            robustness = self._robustness(hypothesis, test, config)
            walk_forward = self._walk_forward(hypothesis, candles, config)
            self.store.research_save("validation", {"experiment_id": row["experiment_id"], "stage": "ROBUSTNESS", "status": robustness["status"], "metrics_json": json.dumps(robustness), "failure_reasons_json": json.dumps(robustness.get("failures", [])), "created_at": _utc_now().isoformat()})
            self.store.research_save("validation", {"experiment_id": row["experiment_id"], "stage": "WALK_FORWARD", "status": walk_forward["status"], "metrics_json": json.dumps(walk_forward), "failure_reasons_json": json.dumps(walk_forward.get("failures", [])), "created_at": _utc_now().isoformat()})
            self.store.research_save("validation", {"experiment_id": row["experiment_id"], "stage": "MULTIPLE_TESTING_TRACKING", "status": "TRACKED", "metrics_json": json.dumps({"experiments_tested_to_date": self.store.research_summary()["experiments"], "correction": "No profitability claim is made from this count alone."}), "failure_reasons_json": json.dumps([]), "created_at": _utc_now().isoformat()})
            self.store.research_save("evidence", {"experiment_id": row["experiment_id"], "evidence_type": "VALIDATION_SUMMARY", "statement": f"{validation_status}: bounded train/out-of-sample, cost-stress, and walk-forward results were recorded for {symbol.upper()} {timeframe.upper()}.", "source_json": json.dumps({"hypothesis_id": hypothesis.hypothesis_id, "sample_candles": len(candles), "out_of_sample_trades": test_result.metrics["sample_size"], "failures": failures}), "created_at": _utc_now().isoformat()})
            self.store.research_save("learning", {"category": "STRATEGY", "subject": hypothesis.family, "observation": f"{hypothesis.name} produced {validation_status} on the bounded {symbol.upper()} {timeframe.upper()} sample.", "evidence_json": json.dumps({"experiment_id": row["experiment_id"], "out_of_sample": test_result.metrics}), "confidence_label": "LIMITED_SAMPLE", "created_at": _utc_now().isoformat()})
            self.store.research_update_experiment(row["experiment_id"], "COMPLETE", 1.0)
            processed.append({"experiment_id": row["experiment_id"], "status": validation_status, "train": train_result.metrics, "out_of_sample": test_result.metrics, "robustness": robustness, "walk_forward": walk_forward, "failures": failures})
        return {"processed": processed, "summary": self.store.research_summary()}

    def _robustness(self, hypothesis: Hypothesis, candles: Sequence[Candle], config: BacktestConfig) -> dict[str, Any]:
        scenarios = []
        for spread_multiplier, slippage_multiplier in ((1.0, 1.0), (1.5, 1.5), (2.0, 2.0)):
            stressed = BacktestConfig(**{**config.to_dict(), "spread_points": config.spread_points * spread_multiplier, "slippage_points": config.slippage_points * slippage_multiplier})
            result = self.engine.run(hypothesis, candles, stressed, f"ROBUSTNESS_{spread_multiplier:g}X")
            scenarios.append(result.metrics)
        failures = ["negative_expectancy_under_cost_stress"] if any(float(item["expectancy"]) <= 0 for item in scenarios) else []
        return {"status": "FAIL" if failures else "PASS", "scenarios": scenarios, "failures": failures}

    def _walk_forward(self, hypothesis: Hypothesis, candles: Sequence[Candle], config: BacktestConfig) -> dict[str, Any]:
        train_size = max(50, int(len(candles) * 0.5))
        test_size = max(20, int(len(candles) * 0.2))
        windows = []
        start = 0
        while start + train_size + test_size <= len(candles):
            result = self.engine.run(hypothesis, candles[start + train_size : start + train_size + test_size], config, "WALK_FORWARD")
            windows.append(result.metrics)
            start += test_size
        failures = []
        if len(windows) < 2:
            failures.append("insufficient_walk_forward_windows")
        if any(item["sample_size"] < 10 for item in windows):
            failures.append("insufficient_trades_in_walk_forward_window")
        status = "INCONCLUSIVE" if failures else ("PASS" if all(item["expectancy"] > 0 for item in windows) else "FAIL")
        return {"status": status, "window_count": len(windows), "windows": windows, "failures": failures}

    @staticmethod
    def _validate(train: BacktestResult, out_of_sample: BacktestResult) -> tuple[str, list[str]]:
        failures: list[str] = []
        metrics = out_of_sample.metrics
        if metrics["data_status"] != "OK":
            failures.append("insufficient_or_invalid_data")
        if metrics["sample_size"] < 30:
            failures.append("fewer_than_30_out_of_sample_trades")
        if metrics["profit_factor"] is None or metrics["profit_factor"] <= 1.0:
            failures.append("non_positive_or_unmeasurable_profit_factor")
        if metrics["expectancy"] <= 0:
            failures.append("non_positive_out_of_sample_expectancy")
        if metrics["max_drawdown_percent"] > 0.25:
            failures.append("drawdown_exceeds_screening_limit")
        return ("RESEARCH_CANDIDATE" if not failures else "INCONCLUSIVE" if len(failures) == 1 and failures[0] == "fewer_than_30_out_of_sample_trades" else "REJECTED"), failures
