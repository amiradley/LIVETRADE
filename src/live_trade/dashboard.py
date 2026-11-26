from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .execution import DemoOnlyExecutionAdapter, ExecutionError
from .finance import FinancialDataService, FinancialSnapshot
from .historical import CollectionScheduler, HistoricalDataEngine
from .intelligence import FinancialIntelligenceService, FinancialIntelligenceSnapshot
from .market_validation import MarketDataValidationError, validate_decision_market_data
from .mt5_client import ReadOnlyMt5Client
from .monitoring import DemoMonitoringWorker
from .research import ResearchWorker
from .research_intelligence import ResearchService
from .session import SessionError, SessionMode, SessionState, TradingSessionController
from .storage import MarketDataStore
from .storage_manager import StorageManager
from .technical import TechnicalAnalyzer
from .trade_decision import build_experimental_decision
from .trial_readiness import build_trial_readiness

LOGGER = logging.getLogger(__name__)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WEB_ROOT = Path(__file__).with_name("web")


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _number(value: Any) -> float | int | None:
    if value is None:
        return None
    return float(value) if isinstance(value, float) else value


def _field(value: Any, status: str, source: str | None = None) -> dict[str, Any]:
    return {"value": _number(value), "status": status, "source": source}


def _converted_field(field: dict[str, Any], rate: Any, quote_currency: str = "MYR") -> dict[str, Any]:
    if rate.rate is None or field["value"] is None:
        return {"value": None, "status": "UNAVAILABLE" if rate.status == "UNAVAILABLE" else "STALE", "source": rate.source}
    return {
        "value": field["value"] * rate.rate,
        "status": "ESTIMATED" if rate.status == "OK" else rate.status,
        "source": rate.source,
        "currency": quote_currency,
    }


def _position_payload(position: Any, rate: Any) -> dict[str, Any]:
    floating = _field(position.floating_pnl, "BROKER_VERIFIED", "MT5 positions_get")
    return {
        "ticket": position.ticket,
        "symbol": position.symbol,
        "side": position.side,
        "volume": position.volume,
        "entry_price": position.entry_price,
        "current_price": position.current_price,
        "floating_pnl": position.floating_pnl,
        "floating_pnl_myr": _converted_field(floating, rate),
        "stop_loss": position.stop_loss,
        "take_profit": position.take_profit,
        "margin": position.margin,
        "opened_at": _iso(position.opened_at),
        "captured_at": _iso(position.captured_at),
        "data_freshness": "LIVE",
        "currency_conversion": "UNAVAILABLE",
    }


def _history_payload(intelligence: FinancialIntelligenceSnapshot) -> dict[str, Any]:
    rate = intelligence.exchange_rate
    return {
        "status": intelligence.history_status,
        "error": intelligence.history_error,
        "raw_deal_count": intelligence.raw_deal_count,
        "results": [
            {
                **trade.to_dict(),
                "net_profit_myr": _converted_field(
                    _field(trade.net_profit, "INTERNALLY_CALCULATED", "aggregated MT5 deal history"), rate
                ),
                "currency": intelligence.exchange_rate.base_currency,
            }
            for trade in intelligence.history
        ],
        "performance": {
            name: {
                **_field(value, "INTERNALLY_CALCULATED", "aggregated MT5 deal history"),
                "myr": _converted_field(
                    _field(value, "INTERNALLY_CALCULATED", "aggregated MT5 deal history"), rate
                ),
            }
            for name, value in intelligence.performance.items()
        },
        "exchange_rate": rate.to_dict(),
        "allocation": dict(intelligence.allocation),
    }


def _snapshot_payload(
    snapshot: FinancialSnapshot,
    account: dict[str, Any],
    intelligence: FinancialIntelligenceSnapshot,
) -> dict[str, Any]:
    rate = intelligence.exchange_rate
    balance = _field(snapshot.balance, "BROKER_VERIFIED", "MT5 account_info")
    equity = _field(snapshot.equity, "BROKER_VERIFIED", "MT5 account_info")
    profit = _field(account.get("profit"), "BROKER_VERIFIED", "MT5 account_info")
    margin_used = _field(snapshot.margin_used, "BROKER_VERIFIED", "MT5 account_info")
    free_margin = _field(snapshot.free_margin, "BROKER_VERIFIED", "MT5 account_info")
    floating = _field(snapshot.floating_pnl, "INTERNALLY_CALCULATED", "sum of MT5 positions")
    allocation = intelligence.allocation
    realised = _field(intelligence.performance.get("cumulative"), "INTERNALLY_CALCULATED", "aggregated MT5 deal history")
    reserved = _field(allocation.get("total_reserved"), "INTERNALLY_CALCULATED", "20% high-water-mark ledger")
    retained = _field(allocation.get("total_retained"), "INTERNALLY_CALCULATED", "80% high-water-mark ledger")
    trading_capital = (
        _field(snapshot.balance - float(allocation.get("total_reserved", 0.0)), "INTERNALLY_CALCULATED", "balance minus savings reserve")
        if snapshot.balance is not None
        else _field(None, "UNAVAILABLE", "balance unavailable")
    )
    return {
        "captured_at": _iso(snapshot.captured_at),
        "account_type": snapshot.account_type,
        "account_currency": snapshot.account_currency,
        "login": account.get("login"),
        "server": account.get("server"),
        "balance": {**balance, "myr": _converted_field(balance, rate)},
        "equity": {**equity, "myr": _converted_field(equity, rate)},
        "profit": {**profit, "myr": _converted_field(profit, rate)},
        "margin_used": {**margin_used, "myr": _converted_field(margin_used, rate)},
        "free_margin": {**free_margin, "myr": _converted_field(free_margin, rate)},
        "margin_level": _field(account.get("margin_level"), "BROKER_VERIFIED", "MT5 account_info"),
        "floating_pnl": {**floating, "myr": _converted_field(floating, rate)},
        "positions": [_position_payload(position, rate) for position in snapshot.positions],
        "position_count": len(snapshot.positions),
        "data_quality": snapshot.data_quality,
        "currency_conversion": {
            "currency": "MYR",
            "rate": rate.rate,
            "source": rate.source,
            "retrieved_at": _iso(rate.retrieved_at),
            "quote_date": rate.quote_date.isoformat() if rate.quote_date else None,
            "status": rate.status,
            "message": rate.error or ("Live rate retrieved." if rate.rate is not None else "Exchange-rate data unavailable."),
        },
        "allocation": {
            "original_capital": _field(None, "PENDING", "future capital ledger"),
            "realised_net_profit": {**realised, "myr": _converted_field(realised, rate)},
            "reserved_savings": {**reserved, "myr": _converted_field(reserved, rate)},
            "retained_trading_profits": {**retained, "myr": _converted_field(retained, rate)},
            "actual_savings_withdrawals": _field(None, "UNAVAILABLE", "not implemented"),
            "trading_capital": {**trading_capital, "myr": _converted_field(trading_capital, rate)},
        },
        "performance": {
            name: {
                **_field(value, "INTERNALLY_CALCULATED", "aggregated MT5 deal history"),
                "myr": _converted_field(_field(value, "INTERNALLY_CALCULATED", "aggregated MT5 deal history"), rate),
            }
            for name, value in intelligence.performance.items()
        },
        "savings_calculation": dict(allocation),
    }


def _session_payload(status: Any, components: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "state": status.state.value,
        "mode": status.mode.value,
        "session_id": status.session_id,
        "changed_at": _iso(status.changed_at),
        "message": status.message,
        "checks": [asdict(check) for check in status.checks],
        "components": components or {},
    }


class DashboardService:
    """Application read model and safe session facade for the local dashboard."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client or ReadOnlyMt5Client(
            mt5_path=settings.mt5_path,
            broker_time=settings.broker_time,
        )
        self.financial = FinancialDataService(self.client)
        self.store = MarketDataStore(settings.db_path)
        self.storage_manager = StorageManager(settings.base_dir)
        self.historical_engine = HistoricalDataEngine(self.client, self.store, settings.broker_time, settings.base_dir)
        self.collection_scheduler = CollectionScheduler(self.store)
        self.research_service = ResearchService(self.store)
        self._storage_cache: dict[str, Any] | None = None
        self._storage_cache_at = 0.0
        self.lock = threading.RLock()
        self.client_lock = threading.RLock()
        self.research_worker = ResearchWorker(
            self.client,
            self.store,
            settings.symbols[0] if settings.symbols else "EURUSD",
            self.client_lock,
            broker_time=settings.broker_time,
        )
        self.intelligence = FinancialIntelligenceService(self.store)
        self.controller = TradingSessionController(
            self.client,
            self.financial,
            self.store,
            mode=SessionMode.DEMO_SHADOW,
            market_symbol=(settings.symbols[0] if settings.symbols else "EURUSD"),
            research_worker=self.research_worker,
        )
        self.last_successful: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.last_execution_result: dict[str, Any] | None = None
        self.demo_entries_enabled = False
        self.demo_monitor = DemoMonitoringWorker(self._run_demo_monitor_cycle)

    def _execution_controls(self) -> dict[str, Any]:
        risk_gate = DemoOnlyExecutionAdapter(
            self.client.api, self.store, broker_offset_seconds=self.settings.broker_time.utc_offset_seconds or 0.0
        ).entry_risk_gate()
        return {
            "new_entries_enabled": self.demo_entries_enabled,
            "new_entries_blocked": not self.demo_entries_enabled,
            "monitoring": self.demo_monitor.health().to_dict(),
            "risk_gate": risk_gate,
        }

    def _run_demo_monitor_cycle(self) -> dict[str, Any]:
        if self.demo_entries_enabled:
            return self.start_demo_execution()
        result = self.analyze_demo_execution()
        controls = self._execution_controls()
        if "risk_gate" in result:
            controls["risk_gate"] = result["risk_gate"]
        return {**result, **controls}

    def monitoring_status(self) -> dict[str, Any]:
        with self.lock:
            return {"mode": SessionMode.DEMO_EXECUTION.value, **self._execution_controls()}

    def start_monitoring(self) -> dict[str, Any]:
        with self.lock:
            health = self.demo_monitor.start()
            return {
                "mode": SessionMode.DEMO_EXECUTION.value,
                "status": "MONITORING",
                "execution_enabled": self.demo_entries_enabled,
                "message": "Persistent DEMO monitoring started; new entries remain blocked by risk gates.",
                **self._execution_controls(),
                "monitoring": health.to_dict(),
            }

    def stop_monitoring(self) -> dict[str, Any]:
        health = self.demo_monitor.stop()
        with self.lock:
            self.demo_entries_enabled = False
            return {
                "mode": SessionMode.DEMO_EXECUTION.value,
                "status": "STOPPED",
                "execution_enabled": False,
                "message": "Persistent DEMO monitoring stopped and new entries remain blocked.",
                **self._execution_controls(),
                "monitoring": health.to_dict(),
            }

    def enable_demo_entries(self) -> dict[str, Any]:
        with self.lock:
            self.demo_entries_enabled = False
            risk_gate = DemoOnlyExecutionAdapter(
                self.client.api, self.store, broker_offset_seconds=self.settings.broker_time.utc_offset_seconds or 0.0
            ).entry_risk_gate()
            return {
                "mode": SessionMode.DEMO_EXECUTION.value,
                "status": "BLOCKED",
                "stage": "RISK_GATE",
                "execution_enabled": False,
                "error": risk_gate["message"],
                **self._execution_controls(),
            }

    def disable_demo_entries(self) -> dict[str, Any]:
        with self.lock:
            self.demo_entries_enabled = False
            return {
                "mode": SessionMode.DEMO_EXECUTION.value,
                "status": "STOPPED",
                "stage": "ENTRY_STOP",
                "execution_enabled": False,
                "message": "New DEMO entries are blocked; position monitoring may continue.",
                **self._execution_controls(),
            }

    def _capture_live(self) -> dict[str, Any]:
        with self.client_lock:
            return self._capture_live_locked()

    def _capture_live_locked(self) -> dict[str, Any]:
        owns_connection = self.controller.status.state is not SessionState.RUNNING
        if owns_connection:
            self.client.connect()
        try:
            terminal = self.client.terminal_info()
            account = self.client.account_info() or {}
            snapshot = self.financial.capture()
            intelligence = self.intelligence.capture(self.client, str(account.get("currency") or "UNKNOWN"))
            snapshot_id = self.store.save_financial_snapshot(snapshot)
            financial = _snapshot_payload(snapshot, account, intelligence)
            payload = {
                "connection": {
                    "status": "CONNECTED",
                    "broker_verified": True,
                    "terminal_connected": bool(terminal.get("connected", True)),
                    "terminal_name": terminal.get("name", "MetaTrader 5"),
                    "terminal_build": terminal.get("build"),
                    "terminal_path": terminal.get("path"),
                    "last_successful_update": financial["captured_at"],
                    "error": None,
                },
                "terminal": terminal,
                "broker_time": self.settings.broker_time.to_dict(),
                "financial": financial,
                "history": _history_payload(intelligence),
                "snapshot_id": snapshot_id,
                "session": _session_payload(self.controller.status, self.controller.component_health()),
            }
            self.last_successful = payload
            self.last_error = None
            return payload
        finally:
            if owns_connection:
                self.client.disconnect()

    def overview(self) -> dict[str, Any]:
        with self.lock:
            try:
                return self._capture_live()
            except Exception as exc:
                self.last_error = str(exc)
                LOGGER.warning("Dashboard live financial refresh failed: %s", exc)
                if self.controller.status.state is SessionState.RUNNING:
                    self.controller.mark_error(str(exc))
                if self.last_successful is not None:
                    stale = json.loads(json.dumps(self.last_successful, default=str))
                    stale["connection"].update(
                        {
                            "status": "STALE",
                            "broker_verified": False,
                            "terminal_connected": False,
                            "error": self.last_error,
                        }
                    )
                    stale["financial"]["data_quality"] = "STALE"
                    for key in (
                        "balance",
                        "equity",
                        "profit",
                        "margin_used",
                        "free_margin",
                        "margin_level",
                        "floating_pnl",
                    ):
                        stale["financial"][key]["status"] = "STALE"
                        if stale["financial"][key].get("myr"):
                            stale["financial"][key]["myr"]["status"] = "STALE"
                    stale["financial"]["positions"] = [
                        {**position, "data_freshness": "STALE"}
                        for position in stale["financial"]["positions"]
                    ]
                    if stale.get("history"):
                        stale["history"]["status"] = "STALE"
                    stale["session"] = _session_payload(self.controller.status, self.controller.component_health())
                    return stale
                return {
                    "connection": {
                        "status": "DISCONNECTED",
                        "broker_verified": False,
                        "terminal_connected": False,
                        "last_successful_update": None,
                        "error": self.last_error,
                    },
                    "terminal": {},
                    "broker_time": self.settings.broker_time.to_dict(),
                    "financial": None,
                    "snapshot_id": None,
                    "session": _session_payload(self.controller.status, self.controller.component_health()),
                }

    def start(self) -> dict[str, Any]:
        with self.lock:
            status = self.controller.start()
            if status.financial is not None:
                account = self.client.account_info() or {}
                snapshot_id = self.store.save_financial_snapshot(status.financial)
                intelligence = self.intelligence.capture(self.client, str(account.get("currency") or "UNKNOWN"))
                payload = _snapshot_payload(status.financial, account, intelligence)
                self.last_successful = {
                    "connection": {"status": "CONNECTED", "broker_verified": True, "error": None},
                    "terminal": self.client.terminal_info(),
                    "broker_time": self.settings.broker_time.to_dict(),
                    "financial": payload,
                    "history": _history_payload(intelligence),
                    "snapshot_id": snapshot_id,
                    "session": _session_payload(status, self.controller.component_health()),
                }
            return self.overview()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            status = self.controller.stop()
            payload = self.overview() if status.state is not SessionState.STOPPED else self.last_successful
            if payload is None:
                payload = self.overview()
            payload["session"] = _session_payload(status, self.controller.component_health())
            return payload

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "session": _session_payload(self.controller.status, self.controller.component_health()),
                "last_error": self.last_error,
                "last_successful_update": (
                    self.last_successful.get("connection", {}).get("last_successful_update")
                    if self.last_successful
                    else None
                ),
            }

    def _read_rows(self, query: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        if not self.settings.db_path.exists():
            return []
        connection = sqlite3.connect(f"file:{self.settings.db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return list(connection.execute(query, params))
        except sqlite3.OperationalError:
            return []
        finally:
            connection.close()

    def scanner(self) -> dict[str, Any]:
        rows = self._read_rows(
            "SELECT r.*, s.discovered_count, s.eligible_count, s.activated_count "
            "FROM scan_results r JOIN scan_runs s ON s.scan_id=r.scan_id "
            "WHERE r.scan_id=(SELECT MAX(scan_id) FROM scan_runs) ORDER BY r.symbol"
        )
        return {
            "status": "AVAILABLE" if rows else "UNAVAILABLE",
            "message": "Latest persisted scanner run." if rows else "No persisted scanner run is available yet.",
            "results": [dict(row) for row in rows],
        }

    def analysis(self) -> dict[str, Any]:
        rows = self._read_rows(
            "SELECT * FROM multi_timeframe_analysis ORDER BY analysis_id DESC LIMIT 20"
        )
        return {
            "status": "AVAILABLE" if rows else "UNAVAILABLE",
            "message": "Latest persisted technical analysis." if rows else "No persisted analysis is available yet.",
            "results": [
                {
                    **dict(row),
                    "score_breakdown": json.loads(row["score_breakdown_json"]),
                    "timeframes": json.loads(row["timeframe_json"]),
                }
                for row in rows
            ],
        }

    def history(self) -> dict[str, Any]:
        with self.lock:
            if self.last_successful is None:
                self.overview()
            if self.last_successful and self.last_successful.get("history"):
                history = self.last_successful["history"]
                if history["status"] == "BROKER_VERIFIED" and not history["results"]:
                    history = {**history, "message": "MT5 returned no closed deals in the configured lookback period.", "results": []}
                return history
            return {"status": "UNAVAILABLE", "message": "Broker deal history is unavailable.", "results": []}

    def storage(self) -> dict[str, Any]:
        with self.lock:
            if self._storage_cache is None or time.monotonic() - self._storage_cache_at >= 60:
                self._storage_cache = self.storage_manager.scan().to_dict()
                self._storage_cache_at = time.monotonic()
            return self._storage_cache

    def historical(self) -> dict[str, Any]:
        with self.lock:
            return {
                "instruments": self.store.instrument_summary(),
                "coverage": self.store.coverage_summary(),
                "collection": self.collection_scheduler.status(),
                "historical": self.store.historical_summary(),
                "batch_diagnostics": self.store.recent_collection_batch_diagnostics(20),
                "timestamp_configuration": self.settings.broker_time.to_dict(),
                "storage": self.storage(),
            }

    def historical_audit(self) -> dict[str, Any]:
        with self.lock, self.client_lock:
            owns_connection = self.controller.status.state is not SessionState.RUNNING
            if owns_connection:
                self.client.connect()
            try:
                symbols = self.settings.symbols or ("EURUSD",)
                self.historical_engine.classify(symbols)
                reports = self.historical_engine.audit(symbols, ("M1", "M5"), 200)
                return {"reports": [report.to_dict() for report in reports], **self.historical()}
            finally:
                if owns_connection:
                    self.client.disconnect()

    def collection_plan(self) -> dict[str, Any]:
        with self.lock, self.client_lock:
            owns_connection = self.controller.status.state is not SessionState.RUNNING
            if owns_connection:
                self.client.connect()
            try:
                symbols = self.settings.symbols or ("EURUSD",)
                plan = self.historical_engine.preview(symbols, ("M1", "M5"), "TIER1", 500)
                return {"items": [item.to_dict() for item in plan], "estimated_bytes": sum(item.estimated_bytes for item in plan), **self.historical()}
            finally:
                if owns_connection:
                    self.client.disconnect()

    def collection_start(self) -> dict[str, Any]:
        with self.lock, self.client_lock:
            owns_connection = self.controller.status.state is not SessionState.RUNNING
            if owns_connection:
                self.client.connect()
            try:
                symbols = self.settings.symbols or ("EURUSD",)
                plan = self.historical_engine.preview(symbols, ("H1", "M15"), "TIER1", None, "INITIAL", 4)
                result = self.historical_engine.collect_session(plan, max_batches=1, batch_size=50, max_total_candles=100, max_duration_seconds=120.0, max_storage_growth_bytes=8 * 1024 * 1024, rate_limit_seconds=0.25)
                return {"result": result, **self.historical()}
            finally:
                if owns_connection:
                    self.client.disconnect()

    def research(self) -> dict[str, Any]:
        with self.lock:
            return {
                "summary": self.store.research_summary(),
                "hypotheses": self.store.research_rows("research_hypotheses"),
                "experiments": self.store.research_rows("research_experiments"),
                "validations": self.store.research_rows("validation_runs"),
                "learning": self.store.research_rows("learning_observations"),
                "data_readiness": self.store.coverage_summary(),
            }

    def research_run(self) -> dict[str, Any]:
        with self.lock:
            symbol = self.settings.symbols[0] if self.settings.symbols else "EURUSD"
            result = self.research_service.run(symbol, "M1", limit=4)
            return {"result": result, **self.research()}

    def start_demo_execution(self) -> dict[str, Any]:
        with self.lock, self.client_lock:
            if not self.demo_entries_enabled:
                result = {
                    "mode": SessionMode.DEMO_EXECUTION.value,
                    "status": "BLOCKED",
                    "stage": "ENTRY_DISABLED",
                    "execution_enabled": False,
                    "error": "New DEMO entries are disabled by the risk gate.",
                    **self._execution_controls(),
                }
                self.last_execution_result = result
                return result
            symbol = self.settings.symbols[0] if self.settings.symbols else "EURUSD"
            timeframe = "M1"
            self.client.connect()
            try:
                adapter = DemoOnlyExecutionAdapter(
                    self.client.api, self.store, broker_offset_seconds=self.settings.broker_time.utc_offset_seconds or 0.0
                )
                try:
                    identity = adapter.verify_demo_identity()
                except ExecutionError as exc:
                    result = {
                        "mode": SessionMode.DEMO_EXECUTION.value,
                        "status": "BLOCKED",
                        "stage": "DEMO_GUARD",
                        "execution_enabled": False,
                        "error": str(exc),
                    }
                    self.last_execution_result = result
                    return result
                risk_gate = adapter.entry_risk_gate()
                if not risk_gate["entries_allowed"]:
                    result = {
                        "mode": SessionMode.DEMO_EXECUTION.value,
                        "status": "BLOCKED",
                        "stage": "RISK_GATE",
                        "execution_enabled": False,
                        "account": identity["account"],
                        "terminal": identity["terminal"],
                        "risk_gate": risk_gate,
                        "error": risk_gate["message"],
                    }
                    self.last_execution_result = result
                    return result
                tick = self.client.get_tick(symbol)
                candles = self.client.get_candles(symbol, timeframe, 100)
                try:
                    market_data = validate_decision_market_data(
                        symbol, timeframe, tick, candles, self.settings.broker_time
                    )
                except MarketDataValidationError as exc:
                    result = {
                        "mode": SessionMode.DEMO_EXECUTION.value,
                        "status": "BLOCKED",
                        "stage": "DATA_QUALITY",
                        "execution_enabled": False,
                        "account": identity["account"],
                        "terminal": identity["terminal"],
                        "error": str(exc),
                        "market_data": {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "timestamp_configuration": self.settings.broker_time.to_dict(),
                            "candle_count": len(candles),
                            "latest_candle_time": candles[-1].time.isoformat() if candles else None,
                        },
                    }
                    self.last_execution_result = result
                    return result
                analysis = TechnicalAnalyzer(minimum_history=50).analyze(symbol, timeframe, candles, closed_only=True)
                decision = build_experimental_decision(
                    analysis, tick, candles, source_candle_time=market_data.source_candle_effective_time
                )
                base = {
                    "mode": SessionMode.DEMO_EXECUTION.value,
                    "execution_enabled": False,
                    "account": identity["account"],
                    "terminal": identity["terminal"],
                    "analysis": analysis.to_dict(),
                    "decision": decision.to_dict(),
                    "market_data": market_data.to_dict(),
                    "risk": adapter.risk_snapshot(),
                    "risk_gate": adapter.entry_risk_gate(identity["account"]),
                }
                try:
                    adapter.verify_execution_permissions()
                except ExecutionError as exc:
                    result = {**base, "status": "BLOCKED", "stage": "PERMISSION_CHECK", "execution": {"status": "BLOCKED", "error": str(exc)}}
                    self.last_execution_result = result
                    return result
                if decision.decision == "NO_TRADE":
                    result = {**base, "status": "NO_TRADE", "stage": "DECISION", "execution": {"status": "NO_TRADE"}}
                    self.last_execution_result = result
                    return result
                try:
                    execution = adapter.submit(decision.to_dict())
                    result = {**base, "status": execution["status"], "stage": "POSITION_OPEN", "execution_enabled": True, "execution": execution}
                except ExecutionError as exc:
                    result = {**base, "status": "BLOCKED", "stage": "EXECUTION", "execution": {"status": "ERROR", "error": str(exc)}}
                self.last_execution_result = result
                return result
            finally:
                self.client.disconnect()

    def analyze_demo_execution(self) -> dict[str, Any]:
        """Run the real dashboard market-analysis path without any order capability."""
        with self.lock, self.client_lock:
            symbol = self.settings.symbols[0] if self.settings.symbols else "EURUSD"
            timeframe = "M1"
            self.client.connect()
            try:
                adapter = DemoOnlyExecutionAdapter(
                    self.client.api, self.store, broker_offset_seconds=self.settings.broker_time.utc_offset_seconds or 0.0
                )
                identity = adapter.verify_demo_identity()
                tick = self.client.get_tick(symbol)
                candles = self.client.get_candles(symbol, timeframe, 100)
                try:
                    market_data = validate_decision_market_data(
                        symbol, timeframe, tick, candles, self.settings.broker_time
                    )
                except MarketDataValidationError as exc:
                    result = {
                        "mode": SessionMode.DEMO_EXECUTION.value,
                        "status": "BLOCKED",
                        "stage": "DATA_QUALITY",
                        "execution_enabled": False,
                        "account": identity["account"],
                        "terminal": identity["terminal"],
                        "error": str(exc),
                        "market_data": {
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "captured_at": datetime.now(timezone.utc).isoformat(),
                            "timestamp_configuration": self.settings.broker_time.to_dict(),
                            "candle_count": len(candles),
                            "latest_candle_time": candles[-1].time.isoformat() if candles else None,
                        },
                        "message": "Analysis blocked; stale or inconsistent market data was not used.",
                    }
                    self.last_execution_result = result
                    return result
                analysis = TechnicalAnalyzer(minimum_history=50).analyze(symbol, timeframe, candles, closed_only=True)
                decision = build_experimental_decision(
                    analysis, tick, candles, source_candle_time=market_data.source_candle_effective_time
                )
                result = {
                    "mode": SessionMode.DEMO_EXECUTION.value,
                    "status": decision.decision,
                    "stage": "DECISION",
                    "execution_enabled": False,
                    "account": identity["account"],
                    "terminal": identity["terminal"],
                    "analysis": analysis.to_dict(),
                    "decision": decision.to_dict(),
                    "market_data": market_data.to_dict(),
                    "risk": adapter.risk_snapshot(),
                    "risk_gate": adapter.entry_risk_gate(identity["account"]),
                    "message": "Analysis-only path; no order capability was invoked.",
                }
                self.last_execution_result = result
                return result
            finally:
                self.client.disconnect()

    def stop_demo_execution(self) -> dict[str, Any]:
        with self.lock:
            self.demo_entries_enabled = False
        health = self.demo_monitor.stop()
        with self.lock, self.client_lock:
            self.client.connect()
            try:
                positions = self.client.positions()
            finally:
                self.client.disconnect()
            status = "STOPPED_WITH_OPEN_POSITIONS" if positions else "STOPPED"
            result = {
                "mode": SessionMode.DEMO_EXECUTION.value,
                "status": status,
                "stage": "STOP",
                "execution_enabled": False,
                "new_entries_blocked": True,
                "positions": list(positions),
                "message": "New demo entries are blocked; existing positions were not closed.",
                **self._execution_controls(),
                "monitoring": health.to_dict(),
            }
            self.last_execution_result = result
            return result

    def execution_status(self) -> dict[str, Any]:
        with self.lock:
            if self.last_execution_result is not None:
                controls = self._execution_controls()
                if "risk_gate" in self.last_execution_result:
                    controls["risk_gate"] = self.last_execution_result["risk_gate"]
                return {**self.last_execution_result, **controls}
            persisted = self.store.execution_orders(limit=1)
            if persisted:
                latest = persisted[0]
                status = str(latest.get("status") or "RECORDED")
                stage = "POSITION_OPEN" if status == "POSITION_OPEN" else "RECORDED"
                return {
                    "mode": SessionMode.DEMO_EXECUTION.value,
                    "status": status,
                    "stage": stage,
                    "execution_enabled": status == "POSITION_OPEN",
                    "execution": {
                        "status": status,
                        "signal_id": latest.get("signal_id"),
                        "symbol": latest.get("symbol"),
                        "direction": latest.get("direction"),
                        "volume": latest.get("volume"),
                        "risk_amount": latest.get("risk_amount"),
                        "order_ticket": latest.get("order_ticket"),
                        "position_ticket": latest.get("position_ticket"),
                        "updated_at": latest.get("updated_at"),
                    },
                    "message": "Persisted execution state restored from SQLite.",
                    **self._execution_controls(),
                }
            return {
                "mode": SessionMode.DEMO_EXECUTION.value,
                "status": "STOPPED",
                "stage": "IDLE",
                "execution_enabled": False,
                "message": "No demo-execution attempt has been started.",
                **self._execution_controls(),
            }

    def trial_readiness(self) -> dict[str, Any]:
        with self.lock:
            return build_trial_readiness(self.store)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "LIVE-TRADING-Dashboard/0.1"

    @property
    def service(self) -> DashboardService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("Dashboard %s", format % args)

    def _send(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._send({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/":
                return self._send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            if path == "/styles.css":
                return self._send_file(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
            if path == "/app.js":
                return self._send_file(WEB_ROOT / "app.js", "text/javascript; charset=utf-8")
            if path == "/readiness.js":
                return self._send_file(WEB_ROOT / "readiness.js", "text/javascript; charset=utf-8")
            if path == "/execution.js":
                return self._send_file(WEB_ROOT / "execution.js", "text/javascript; charset=utf-8")
            if path == "/api/overview":
                return self._send(self.service.overview())
            if path == "/api/status":
                return self._send(self.service.status())
            if path == "/api/scanner":
                return self._send(self.service.scanner())
            if path == "/api/analysis":
                return self._send(self.service.analysis())
            if path == "/api/history":
                return self._send(self.service.history())
            if path == "/api/storage":
                return self._send(self.service.storage())
            if path == "/api/historical":
                return self._send(self.service.historical())
            if path == "/api/research":
                return self._send(self.service.research())
            if path == "/api/trial-readiness":
                return self._send(self.service.trial_readiness())
            if path == "/api/execution":
                return self._send(self.service.execution_status())
            if path == "/api/monitoring":
                return self._send(self.service.monitoring_status())
            return self._send({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            LOGGER.exception("Dashboard request failed")
            self._send({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/api/session/start":
                return self._send(self.service.start())
            if path == "/api/session/stop":
                return self._send(self.service.stop())
            if path == "/api/session/start-demo-execution":
                return self._send(self.service.start_demo_execution())
            if path == "/api/session/analyze-demo-execution":
                return self._send(self.service.analyze_demo_execution())
            if path == "/api/session/stop-demo-execution":
                return self._send(self.service.stop_demo_execution())
            if path == "/api/monitoring/start":
                return self._send(self.service.start_monitoring())
            if path == "/api/monitoring/stop":
                return self._send(self.service.stop_monitoring())
            if path == "/api/monitoring/enable-entries":
                return self._send(self.service.enable_demo_entries())
            if path == "/api/monitoring/disable-entries":
                return self._send(self.service.disable_demo_entries())
            if path == "/api/historical/audit":
                return self._send(self.service.historical_audit())
            if path == "/api/historical/plan":
                return self._send(self.service.collection_plan())
            if path == "/api/historical/start":
                return self._send(self.service.collection_start())
            if path == "/api/historical/pause":
                self.service.collection_scheduler.pause()
                return self._send(self.service.historical())
            if path == "/api/historical/resume":
                self.service.collection_scheduler.resume()
                return self._send(self.service.historical())
            if path == "/api/historical/cancel":
                self.service.collection_scheduler.cancel()
                return self._send(self.service.historical())
            if path == "/api/research/start":
                return self._send(self.service.research_run())
            return self._send({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except SessionError as exc:
            self._send({"error": str(exc), "session": _session_payload(self.service.controller.status, self.service.controller.component_health())}, HTTPStatus.CONFLICT)
        except Exception as exc:
            LOGGER.exception("Dashboard command failed")
            self._send({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


class DashboardServer(ThreadingHTTPServer):
    # A second instance must fail rather than silently creating a competing local API.
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], service: DashboardService) -> None:
        self.service = service
        super().__init__(address, DashboardHandler)


def serve_dashboard(
    settings: Settings,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    open_browser: bool = False,
) -> None:
    service = DashboardService(settings)
    try:
        server = DashboardServer((host, port), service)
    except OSError as exc:
        raise RuntimeError(
            f"Dashboard could not bind to http://{host}:{port}. "
            "Another dashboard may already be running or the port is unavailable."
        ) from exc
    url = f"http://{host}:{port}/"
    LOGGER.info("Dashboard listening at %s", url)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Dashboard stopping")
    finally:
        server.server_close()

