from __future__ import annotations

import json
import logging
import sqlite3
import threading
import webbrowser
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .finance import FinancialDataService, FinancialSnapshot
from .mt5_client import ReadOnlyMt5Client
from .session import SessionError, SessionMode, SessionState, TradingSessionController
from .storage import MarketDataStore

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


def _position_payload(position: Any) -> dict[str, Any]:
    return {
        "ticket": position.ticket,
        "symbol": position.symbol,
        "side": position.side,
        "volume": position.volume,
        "entry_price": position.entry_price,
        "current_price": position.current_price,
        "floating_pnl": position.floating_pnl,
        "floating_pnl_myr": None,
        "stop_loss": position.stop_loss,
        "take_profit": position.take_profit,
        "margin": position.margin,
        "opened_at": _iso(position.opened_at),
        "captured_at": _iso(position.captured_at),
        "data_freshness": "LIVE",
        "currency_conversion": "UNAVAILABLE",
    }


def _snapshot_payload(snapshot: FinancialSnapshot, account: dict[str, Any]) -> dict[str, Any]:
    return {
        "captured_at": _iso(snapshot.captured_at),
        "account_type": snapshot.account_type,
        "account_currency": snapshot.account_currency,
        "login": account.get("login"),
        "server": account.get("server"),
        "balance": _field(snapshot.balance, "BROKER_VERIFIED", "MT5 account_info"),
        "equity": _field(snapshot.equity, "BROKER_VERIFIED", "MT5 account_info"),
        "profit": _field(account.get("profit"), "BROKER_VERIFIED", "MT5 account_info"),
        "margin_used": _field(snapshot.margin_used, "BROKER_VERIFIED", "MT5 account_info"),
        "free_margin": _field(snapshot.free_margin, "BROKER_VERIFIED", "MT5 account_info"),
        "margin_level": _field(account.get("margin_level"), "BROKER_VERIFIED", "MT5 account_info"),
        "floating_pnl": _field(snapshot.floating_pnl, "INTERNALLY_CALCULATED", "sum of MT5 positions"),
        "positions": [_position_payload(position) for position in snapshot.positions],
        "position_count": len(snapshot.positions),
        "data_quality": snapshot.data_quality,
        "currency_conversion": {
            "currency": "MYR",
            "rate": None,
            "source": None,
            "status": "UNAVAILABLE",
            "message": "No exchange-rate source is configured; broker currency is shown unchanged.",
        },
        "allocation": {
            "original_capital": _field(None, "PENDING", "future accounting ledger"),
            "realised_net_profit": _field(None, "PENDING", "future deal-history ledger"),
            "reserved_savings": _field(None, "PENDING", "future accounting ledger"),
            "retained_trading_profits": _field(None, "PENDING", "future accounting ledger"),
            "actual_savings_withdrawals": _field(None, "UNAVAILABLE", "not implemented"),
            "trading_capital": _field(None, "PENDING", "future accounting ledger"),
        },
    }


def _session_payload(status: Any) -> dict[str, Any]:
    return {
        "state": status.state.value,
        "mode": status.mode.value,
        "session_id": status.session_id,
        "changed_at": _iso(status.changed_at),
        "message": status.message,
        "checks": [asdict(check) for check in status.checks],
    }


class DashboardService:
    """Application read model and safe session facade for the local dashboard."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = ReadOnlyMt5Client(mt5_path=settings.mt5_path)
        self.financial = FinancialDataService(self.client)
        self.store = MarketDataStore(settings.db_path)
        self.controller = TradingSessionController(
            self.client,
            self.financial,
            self.store,
            mode=SessionMode.DEMO_SHADOW,
            market_symbol=(settings.symbols[0] if settings.symbols else "EURUSD"),
        )
        self.lock = threading.RLock()
        self.last_successful: dict[str, Any] | None = None
        self.last_error: str | None = None

    def _capture_live(self) -> dict[str, Any]:
        owns_connection = self.controller.status.state is not SessionState.RUNNING
        if owns_connection:
            self.client.connect()
        try:
            terminal = self.client.terminal_info()
            account = self.client.account_info() or {}
            snapshot = self.financial.capture()
            snapshot_id = self.store.save_financial_snapshot(snapshot)
            financial = _snapshot_payload(snapshot, account)
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
                "financial": financial,
                "snapshot_id": snapshot_id,
                "session": _session_payload(self.controller.status),
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
                    stale["financial"]["positions"] = [
                        {**position, "data_freshness": "STALE"}
                        for position in stale["financial"]["positions"]
                    ]
                    stale["session"] = _session_payload(self.controller.status)
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
                    "financial": None,
                    "snapshot_id": None,
                    "session": _session_payload(self.controller.status),
                }

    def start(self) -> dict[str, Any]:
        with self.lock:
            status = self.controller.start()
            if status.financial is not None:
                account = self.client.account_info() or {}
                snapshot_id = self.store.save_financial_snapshot(status.financial)
                payload = _snapshot_payload(status.financial, account)
                self.last_successful = {
                    "connection": {"status": "CONNECTED", "broker_verified": True, "error": None},
                    "terminal": self.client.terminal_info(),
                    "financial": payload,
                    "snapshot_id": snapshot_id,
                    "session": _session_payload(status),
                }
            return self.overview()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            status = self.controller.stop()
            payload = self.overview() if status.state is not SessionState.STOPPED else self.last_successful
            if payload is None:
                payload = self.overview()
            payload["session"] = _session_payload(status)
            return payload

    def status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "session": _session_payload(self.controller.status),
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
        return {
            "status": "UNAVAILABLE",
            "message": "Verified broker deal-history reconciliation is a later milestone.",
            "results": [],
        }


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
            return self._send({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except SessionError as exc:
            self._send({"error": str(exc), "session": _session_payload(self.service.controller.status)}, HTTPStatus.CONFLICT)
        except Exception as exc:
            LOGGER.exception("Dashboard command failed")
            self._send({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True

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

