from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .finance import FinancialDataService, FinancialSnapshot


class SessionState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"
    UNRESOLVED_POSITIONS = "UNRESOLVED_POSITIONS"


class SessionMode(str, Enum):
    RESEARCH = "RESEARCH"
    DEMO_SHADOW = "DEMO_SHADOW"


class SessionError(RuntimeError):
    """Raised when a session transition is unsafe or invalid."""


@dataclass(frozen=True)
class SessionCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class SessionStatus:
    state: SessionState
    mode: SessionMode
    session_id: int | None
    changed_at: datetime
    checks: tuple[SessionCheck, ...] = ()
    message: str = ""
    financial: FinancialSnapshot | None = None


class TradingSessionController:
    """Safe research/demo-shadow lifecycle; it deliberately has no order APIs."""

    def __init__(
        self,
        client: Any,
        financial_service: FinancialDataService,
        store: Any,
        mode: SessionMode = SessionMode.RESEARCH,
        market_symbol: str = "EURUSD",
        research_worker: Any | None = None,
    ) -> None:
        self.client = client
        self.financial_service = financial_service
        self.store = store
        self.mode = mode
        self.market_symbol = market_symbol
        self.research_worker = research_worker
        self.status = SessionStatus(SessionState.STOPPED, mode, None, datetime.now(timezone.utc))

    def start(self) -> SessionStatus:
        if self.status.state in {SessionState.STARTING, SessionState.RUNNING, SessionState.STOPPING}:
            raise SessionError(f"Cannot start while session is {self.status.state.value}")
        self.status = SessionStatus(SessionState.STARTING, self.mode, None, datetime.now(timezone.utc))
        checks: list[SessionCheck] = []
        session_id: int | None = None
        try:
            self.client.connect()
            terminal = self.client.terminal_info()
            checks.append(SessionCheck("mt5_terminal", bool(terminal.get("connected", True)), "MT5 connected"))
            account = self.client.account_info()
            account_ok = account is not None and account.get("currency") is not None
            checks.append(SessionCheck("account", account_ok, "Account information available" if account_ok else "Account information unavailable"))
            if account is not None and account.get("trade_mode") not in (None, 0):
                raise SessionError("Stage 1 only permits a MetaQuotes demo account")
            tick = self.client.get_tick(self.market_symbol)
            checks.append(SessionCheck("market_data", tick.bid >= 0 and tick.ask >= tick.bid, f"{self.market_symbol} tick available"))
            self.client.get_candles(self.market_symbol, "M1", 2)
            checks.append(SessionCheck("historical_data", True, "M1 candles available"))
            with self.store.connect():
                checks.append(SessionCheck("database", True, "SQLite available"))
            financial = self.financial_service.capture()
            session_id = self.store.create_session(
                mode=self.mode.value,
                state=SessionState.RUNNING.value,
                started_at=financial.captured_at,
                starting_balance=financial.balance,
                starting_equity=financial.equity,
            )
            self.status = SessionStatus(
                SessionState.RUNNING,
                self.mode,
                session_id,
                datetime.now(timezone.utc),
                tuple(checks),
                "Research/demo-shadow session started; execution remains disabled.",
                financial,
            )
            if self.research_worker is not None:
                self.research_worker.start()
            return self.status
        except Exception as exc:
            if session_id is not None:
                self.store.update_session(
                    session_id,
                    state=SessionState.ERROR.value,
                    ending_balance=None,
                    ending_equity=None,
                    stopped_at=None,
                    message=str(exc),
                )
            self.client.disconnect()
            self.status = SessionStatus(
                SessionState.ERROR,
                self.mode,
                session_id,
                datetime.now(timezone.utc),
                tuple(checks),
                str(exc),
            )
            if isinstance(exc, SessionError):
                raise
            raise SessionError(str(exc)) from exc

    def stop(self) -> SessionStatus:
        if self.status.state is SessionState.ERROR:
            if self.research_worker is not None:
                self.research_worker.stop()
            self.client.disconnect()
            if self.status.session_id is not None:
                self.store.update_session(
                    self.status.session_id,
                    state=SessionState.STOPPED.value,
                    ending_balance=None,
                    ending_equity=None,
                    stopped_at=datetime.now(timezone.utc),
                    message="Session stopped after an integration error; no execution was enabled.",
                )
            self.status = SessionStatus(
                SessionState.STOPPED,
                self.mode,
                self.status.session_id,
                datetime.now(timezone.utc),
                message="Session stopped after an integration error.",
            )
            return self.status
        if self.status.state not in {SessionState.RUNNING, SessionState.UNRESOLVED_POSITIONS}:
            raise SessionError(f"Cannot stop while session is {self.status.state.value}")
        self.status = SessionStatus(
            SessionState.STOPPING, self.mode, self.status.session_id, datetime.now(timezone.utc)
        )
        if self.research_worker is not None:
            self.research_worker.stop()
        try:
            financial = self.financial_service.capture()
        except Exception as exc:
            self.mark_error(str(exc))
            raise SessionError(str(exc)) from exc
        if financial.positions:
            self.status = SessionStatus(
                SessionState.UNRESOLVED_POSITIONS,
                self.mode,
                self.status.session_id,
                datetime.now(timezone.utc),
                message="Open positions remain; execution is disabled, so no closure was attempted.",
                financial=financial,
            )
            self.store.update_session(
                self.status.session_id,
                state=self.status.state.value,
                ending_balance=financial.balance,
                ending_equity=financial.equity,
                stopped_at=None,
                message=self.status.message,
            )
            return self.status
        self.client.disconnect()
        self.store.update_session(
            self.status.session_id,
            state=SessionState.STOPPED.value,
            ending_balance=financial.balance,
            ending_equity=financial.equity,
            stopped_at=datetime.now(timezone.utc),
            message="Session stopped with no open positions; no execution was enabled.",
        )
        self.status = SessionStatus(
            SessionState.STOPPED,
            self.mode,
            self.status.session_id,
            datetime.now(timezone.utc),
            message="Session stopped.",
            financial=financial,
        )
        return self.status

    def mark_error(self, message: str) -> SessionStatus:
        if self.research_worker is not None:
            self.research_worker.stop()
        self.client.disconnect()
        if self.status.session_id is not None:
            self.store.update_session(
                self.status.session_id,
                state=SessionState.ERROR.value,
                ending_balance=None,
                ending_equity=None,
                stopped_at=None,
                message=message,
            )
        self.status = SessionStatus(
            SessionState.ERROR,
            self.mode,
            self.status.session_id,
            datetime.now(timezone.utc),
            message=message,
        )
        return self.status

    def component_health(self) -> dict[str, Any]:
        if self.research_worker is None:
            return {
                "scanner": {"state": "NOT_CONFIGURED"},
                "technical_analysis": {"state": "NOT_CONFIGURED"},
                "market_monitoring": {"state": "NOT_CONFIGURED"},
            }
        worker = self.research_worker.health().to_dict()
        state = worker["state"]
        return {
            "scanner": {**worker, "state": state},
            "technical_analysis": {**worker, "state": state},
            "market_monitoring": {**worker, "state": state},
        }
