from datetime import datetime, timezone

import pytest

from live_trade.config import Settings
from live_trade.dashboard import DashboardService
from live_trade.execution import DemoAccountError, DemoOnlyExecutionAdapter, OrderValidationError, RiskLimitError
from live_trade.mt5_client import ReadOnlyMt5Client
from live_trade.market_validation import MarketDataValidationError, validate_decision_market_data
from live_trade.storage import MarketDataStore
from live_trade.trade_decision import build_experimental_decision
from live_trade.models import Candle, Tick
from live_trade.technical import TechnicalAnalyzer
from live_trade.timestamping import BrokerTimeConfig


class ExecutionApi:
    TIMEFRAME_M1 = 1
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1

    def __init__(self, *, demo=True, terminal_allowed=True):
        self.demo = demo
        self.terminal_allowed = terminal_allowed
        self.order_send_calls = 0
        self.positions = []
        self.candle_base = None
        self.last_request = None

    def initialize(self, **kwargs):
        return True

    def shutdown(self):
        return None

    def account_info(self):
        return {
            "trade_mode": 0 if self.demo else 2,
            "server": "MetaQuotes-Demo" if self.demo else "Broker-Live",
            "currency": "USD",
            "trade_allowed": True,
            "trade_expert": True,
            "balance": 10000.0,
            "equity": 10000.0,
        }

    def terminal_info(self):
        return {"connected": True, "trade_allowed": self.terminal_allowed, "tradeapi_disabled": False}

    def symbol_info(self, symbol):
        return {
            "name": symbol,
            "visible": True,
            "trade_mode": 4,
            "point": 0.00001,
            "trade_tick_size": 0.00001,
            "trade_tick_value": 1.0,
            "trade_tick_value_loss": 1.0,
            "volume_min": 0.01,
            "volume_max": 100.0,
            "volume_step": 0.01,
            "filling_mode": 1,
        }

    def symbol_info_tick(self, symbol):
        return {"bid": 1.1000, "ask": 1.1002, "time": int(datetime.now(timezone.utc).timestamp())}

    def positions_get(self, **kwargs):
        return tuple(self.positions)

    def orders_get(self):
        return ()

    def order_check(self, request):
        return {"retcode": 0, "comment": "check passed"}

    def order_send(self, request):
        self.order_send_calls += 1
        self.last_request = dict(request)
        self.positions = [{
            "ticket": 456,
            "symbol": request["symbol"],
            "volume": request["volume"],
            "sl": request["sl"],
            "tp": request["tp"],
            "price_open": request["price"],
        }]
        return {"retcode": 10009, "order": 123, "position": 456, "comment": "done"}

    def last_error(self):
        return (0, "ok")

    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        base = self.candle_base or (int(datetime.now(timezone.utc).timestamp()) - (count - 1) * 60)
        return [
            {
                "time": base + index * 60,
                "open": 1.1 + index * 0.00001,
                "high": 1.101 + index * 0.00001,
                "low": 1.099 + index * 0.00001,
                "close": 1.1005 + index * 0.00001,
                "tick_volume": 10,
                "spread": 2,
                "real_volume": 0,
            }
            for index in range(count)
        ]


def decision_payload(signal_id="signal-test"):
    return {
        "signal_id": signal_id,
        "decision": "BUY",
        "symbol": "EURUSD",
        "timeframe": "M1",
        "entry": 1.1002,
        "stop_loss": 1.0992,
        "take_profit": 1.1022,
    }


def test_demo_guard_rejects_non_demo_accounts(tmp_path):
    api = ExecutionApi(demo=False)
    adapter = DemoOnlyExecutionAdapter(api, MarketDataStore(tmp_path / "execution.sqlite3"))

    with pytest.raises(DemoAccountError):
        adapter.verify_demo_identity()


def test_terminal_permission_blocks_before_order_submission(tmp_path):
    api = ExecutionApi(terminal_allowed=False)
    adapter = DemoOnlyExecutionAdapter(api, MarketDataStore(tmp_path / "execution.sqlite3"))

    with pytest.raises(Exception, match="automated trading permission"):
        adapter.submit(decision_payload())
    assert api.order_send_calls == 0


def test_missing_stop_and_minimum_lot_are_rejected(tmp_path):
    api = ExecutionApi()
    adapter = DemoOnlyExecutionAdapter(api, MarketDataStore(tmp_path / "execution.sqlite3"))

    missing_stop = decision_payload("signal-missing-stop")
    missing_stop["stop_loss"] = None
    with pytest.raises(OrderValidationError, match="stop-loss"):
        adapter.submit(missing_stop)
    with pytest.raises(RiskLimitError):
        adapter.calculate_volume("EURUSD", 1.1002, 1.10019, 0.01)


def test_duplicate_signal_is_rejected_after_broker_confirmation(tmp_path):
    api = ExecutionApi()
    adapter = DemoOnlyExecutionAdapter(api, MarketDataStore(tmp_path / "execution.sqlite3"))

    result = adapter.submit(decision_payload())
    assert result["status"] == "POSITION_OPEN"
    assert result["position"]["sl"] == 1.0992
    assert api.last_request["comment"] == "LT-DEMO"
    assert api.last_request["type_filling"] == api.ORDER_FILLING_FOK
    with pytest.raises(OrderValidationError, match="Duplicate signal"):
        adapter.submit(decision_payload())
    assert api.order_send_calls == 1


def test_reconcile_closed_deal_is_position_scoped_and_idempotent(tmp_path):
    api = ExecutionApi()
    adapter = DemoOnlyExecutionAdapter(api, MarketDataStore(tmp_path / "execution.sqlite3"))
    adapter.submit(decision_payload("signal-close-test"))

    def history_deals_get(*args, **kwargs):
        assert kwargs.get("position") == 456
        return (
            {"ticket": 1, "position_id": 456, "entry": 0, "profit": 0.0, "commission": 0.0, "swap": 0.0, "fee": 0.0},
            {"ticket": 2, "position_id": 456, "entry": 1, "profit": 4.62, "commission": 0.0, "swap": 0.0, "fee": 0.0},
        )

    api.history_deals_get = history_deals_get
    api.positions = []
    adapter.reconcile_closed_deals()
    adapter.reconcile_closed_deals()

    row = adapter.store.execution_order("signal-close-test")
    assert row["status"] == "CLOSED"
    assert row["realized_pnl"] == 4.62
    assert len(adapter.store.execution_orders()) == 1


def test_decision_schema_returns_no_trade_for_insufficient_history():
    candles = tuple(
        Candle("EURUSD", "M1", datetime(2026, 1, 1, tzinfo=timezone.utc).replace(minute=index), 1.1, 1.101, 1.099, 1.1005, 10)
        for index in range(10)
    )
    analysis = TechnicalAnalyzer(minimum_history=50).analyze("EURUSD", "M1", candles)
    tick = Tick("EURUSD", candles[-1].time, 1.1, 1.1002, 1.1001, 10)

    decision = build_experimental_decision(analysis, tick, candles)

    assert decision.decision == "NO_TRADE"
    assert decision.label == "EXPERIMENTAL — UNVALIDATED"
    assert decision.entry is None


def test_dashboard_demo_start_reports_permission_block_without_order(tmp_path):
    api = ExecutionApi(terminal_allowed=False)
    settings = Settings(
        tmp_path, None, tmp_path / "dashboard.sqlite3", tmp_path / "dashboard.jsonl", ("EURUSD",),
        broker_time=BrokerTimeConfig.with_offset(0),
    )
    service = DashboardService(settings, client=ReadOnlyMt5Client(api=api))

    result = service.start_demo_execution()

    assert result["mode"] == "DEMO_EXECUTION"
    assert result["status"] == "BLOCKED"
    assert result["stage"] == "PERMISSION_CHECK"
    assert result["execution_enabled"] is False
    assert result["decision"]["label"] == "EXPERIMENTAL — UNVALIDATED"
    assert api.order_send_calls == 0


def test_dashboard_analysis_only_path_never_checks_or_submits_orders(tmp_path):
    api = ExecutionApi(terminal_allowed=False)
    settings = Settings(
        tmp_path, None, tmp_path / "dashboard.sqlite3", tmp_path / "dashboard.jsonl", ("EURUSD",),
        broker_time=BrokerTimeConfig.with_offset(0),
    )
    service = DashboardService(settings, client=ReadOnlyMt5Client(api=api))

    result = service.analyze_demo_execution()

    assert result["stage"] == "DECISION"
    assert result["execution_enabled"] is False
    assert result["decision"]["decision"] in {"BUY", "SELL", "NO_TRADE"}
    assert api.order_send_calls == 0


def test_demo_analysis_blocks_stale_broker_candles_before_decision(tmp_path):
    api = ExecutionApi()
    api.candle_base = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    settings = Settings(
        tmp_path,
        None,
        tmp_path / "dashboard.sqlite3",
        tmp_path / "dashboard.jsonl",
        ("EURUSD",),
        broker_time=BrokerTimeConfig.metaquotes_demo_default(),
    )
    service = DashboardService(settings, client=ReadOnlyMt5Client(api=api, broker_time=settings.broker_time))

    result = service.analyze_demo_execution()

    assert result["status"] == "BLOCKED"
    assert result["stage"] == "DATA_QUALITY"
    assert "stale" in result["error"]
    assert result["execution_enabled"] is False
    assert api.order_send_calls == 0


def test_market_data_validation_applies_broker_offset_and_uses_closed_source_candle():
    broker_time = BrokerTimeConfig.with_offset(2)
    captured = datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    candles = tuple(
        Candle("EURUSD", "M1", datetime(2026, 1, 1, 2, minute=index, tzinfo=timezone.utc), 1.1, 1.101, 1.099, 1.1005, 10)
        for index in (0, 1, 2)
    )
    tick = Tick("EURUSD", datetime(2026, 1, 1, 2, 2, tzinfo=timezone.utc), 1.1, 1.1002, 1.1, 10)

    evidence = validate_decision_market_data("EURUSD", "M1", tick, candles, broker_time, captured)

    assert evidence.tick_effective_time == datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc)
    assert evidence.source_candle_effective_time == datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    assert evidence.source_candle_age_seconds == 60


def test_market_data_validation_rejects_future_candles():
    broker_time = BrokerTimeConfig.with_offset(0)
    captured = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = tuple(
        Candle("EURUSD", "M1", datetime(2026, 1, 1, minute=index, tzinfo=timezone.utc), 1.1, 1.101, 1.099, 1.1005, 10)
        for index in (0, 1, 2)
    )
    tick = Tick("EURUSD", captured, 1.1, 1.1002, 1.1, 10)

    with pytest.raises(MarketDataValidationError, match="future"):
        validate_decision_market_data("EURUSD", "M1", tick, candles, broker_time, captured)


def test_decision_reports_effective_source_time_and_preserves_raw_label():
    candles = tuple(
        Candle("EURUSD", "M1", datetime(2026, 1, 1, 2, minute=index, tzinfo=timezone.utc), 1.1, 1.101, 1.099, 1.1005, 10)
        for index in range(60)
    )
    analysis = TechnicalAnalyzer(minimum_history=50).analyze("EURUSD", "M1", candles, closed_only=True)
    tick = Tick("EURUSD", candles[-1].time, 1.1, 1.1002, 1.1, 10)
    effective = datetime(2026, 1, 1, 0, 58, tzinfo=timezone.utc)

    decision = build_experimental_decision(analysis, tick, candles, source_candle_time=effective)

    assert decision.source_candle_time == effective
    assert decision.market_conditions["source_candle_time_raw"] == candles[-2].time.isoformat()
    assert decision.market_conditions["source_candle_time_effective"] == effective.isoformat()
