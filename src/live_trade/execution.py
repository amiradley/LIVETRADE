from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .mt5_client import _record_to_dict


class ExecutionError(RuntimeError):
    """Base class for guarded demo execution failures."""


class DemoAccountError(ExecutionError):
    """Raised when the terminal/account cannot be positively identified as demo."""


class RiskLimitError(ExecutionError):
    """Raised when an order would exceed the experimental risk boundary."""


class OrderValidationError(ExecutionError):
    """Raised when a broker order request is incomplete or unsafe."""


@dataclass(frozen=True)
class ExperimentalRiskBudget:
    allocation_usd: float = 100.0 / 4.0935
    max_risk_per_trade_usd: float = (100.0 / 4.0935) * 0.10
    max_open_positions: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "allocation_usd": self.allocation_usd,
            "max_risk_per_trade_usd": self.max_risk_per_trade_usd,
            "max_open_positions": self.max_open_positions,
            "policy": "10% of the RM100-equivalent allocation per trade; one position maximum.",
        }


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise OrderValidationError(f"{label} is not numeric: {value!r}") from exc
    if not math.isfinite(result):
        raise OrderValidationError(f"{label} is not finite")
    return result


class DemoOnlyExecutionAdapter:
    """Guarded MT5 order adapter. It rejects every non-demo or unsafe request."""

    def __init__(self, api: Any, store: Any, budget: ExperimentalRiskBudget | None = None) -> None:
        self.api = api
        self.store = store
        self.budget = budget or ExperimentalRiskBudget()

    def _account(self) -> dict[str, Any]:
        return _record_to_dict(self.api.account_info())

    def _terminal(self) -> dict[str, Any]:
        return _record_to_dict(self.api.terminal_info())

    def verify_demo_identity(self) -> dict[str, Any]:
        account = self._account()
        terminal = self._terminal()
        if not account:
            raise DemoAccountError("MT5 account information is unavailable")
        if int(account.get("trade_mode", -1)) != 0:
            raise DemoAccountError("Execution requires ACCOUNT_TRADE_MODE_DEMO")
        server = str(account.get("server") or "")
        if "demo" not in server.lower():
            raise DemoAccountError(f"Execution requires a demo server; received {server!r}")
        if not bool(terminal.get("connected", True)):
            raise DemoAccountError("MT5 terminal is not connected")
        return {"account": account, "terminal": terminal}

    def verify_execution_permissions(self) -> dict[str, Any]:
        evidence = self.verify_demo_identity()
        account = evidence["account"]
        terminal = evidence["terminal"]
        if not bool(account.get("trade_allowed")):
            raise ExecutionError("Demo account trading permission is disabled")
        if not bool(account.get("trade_expert")):
            raise ExecutionError("Demo expert trading permission is disabled")
        if not bool(terminal.get("trade_allowed")):
            raise ExecutionError("MT5 terminal automated trading permission is disabled")
        if bool(terminal.get("tradeapi_disabled")):
            raise ExecutionError("MT5 trade API is disabled by the terminal")
        return evidence

    def _positions(self) -> tuple[dict[str, Any], ...]:
        values = self.api.positions_get()
        if values is None:
            raise ExecutionError(f"MT5 positions_get failed: {_record_to_dict(self.api.last_error())}")
        return tuple(_record_to_dict(item) for item in values)

    def _orders(self) -> tuple[dict[str, Any], ...]:
        getter = getattr(self.api, "orders_get", None)
        if getter is None:
            return ()
        values = getter()
        if values is None:
            raise ExecutionError(f"MT5 orders_get failed: {_record_to_dict(self.api.last_error())}")
        return tuple(_record_to_dict(item) for item in values)

    def risk_snapshot(self) -> dict[str, Any]:
        self.reconcile_closed_deals()
        rows = self.store.execution_orders()
        realized_losses = sum(max(0.0, -float(row["realized_pnl"])) for row in rows if row.get("realized_pnl") is not None)
        reserved_risk = sum(
            float(row.get("risk_amount") or 0.0)
            for row in rows
            if row.get("status") in {"PENDING", "BROKER_CONFIRMED", "POSITION_OPEN", "UNCERTAIN"}
        )
        positions = self._positions()
        used = realized_losses + reserved_risk
        return {
            **self.budget.to_dict(),
            "realized_losses_usd": realized_losses,
            "reserved_open_risk_usd": reserved_risk,
            "used_risk_usd": used,
            "remaining_usd": max(0.0, self.budget.allocation_usd - used),
            "broker_open_positions": len(positions),
            "pending_orders": len(self._orders()),
        }

    def reconcile_closed_deals(self) -> None:
        getter = getattr(self.api, "history_deals_get", None)
        if getter is None:
            return
        end = datetime.now(timezone.utc)
        open_positions = self._positions()
        open_position_tickets = {int(item.get("ticket")) for item in open_positions if item.get("ticket") is not None}
        for row in self.store.execution_orders():
            position_ticket = row.get("position_ticket") or row.get("order_ticket")
            if position_ticket is None or int(position_ticket) in open_position_tickets:
                continue
            try:
                values = getter(position=int(position_ticket)) or ()
            except (TypeError, Exception):
                try:
                    values = getter(end - timedelta(days=365), end) or ()
                except Exception:
                    continue
            matching: list[dict[str, Any]] = []
            for value in values:
                deal = _record_to_dict(value)
                if int(deal.get("position_id") or 0) == int(position_ticket):
                    matching.append(deal)
            closing = [deal for deal in matching if int(deal.get("entry") or 0) in {1, 2, 3}]
            if not closing:
                continue
            total = sum(
                sum(float(deal.get(field) or 0.0) for field in ("profit", "commission", "swap", "fee"))
                for deal in matching
            )
            if row.get("status") == "CLOSED" and row.get("realized_pnl") is not None and abs(float(row["realized_pnl"]) - total) <= 1e-9:
                continue
            self.store.update_execution_order(row["signal_id"], realized_pnl=total, status="CLOSED")

    def _symbol_info(self, symbol: str) -> dict[str, Any]:
        value = self.api.symbol_info(symbol)
        info = _record_to_dict(value)
        if not info or not bool(info.get("visible", True)):
            raise OrderValidationError(f"Symbol {symbol} is unavailable or not visible")
        if int(info.get("trade_mode", 0)) == 0:
            raise OrderValidationError(f"Symbol {symbol} is not tradeable")
        return info

    def _tick(self, symbol: str) -> dict[str, Any]:
        value = self.api.symbol_info_tick(symbol)
        tick = _record_to_dict(value)
        if not tick:
            raise OrderValidationError(f"No current tick is available for {symbol}")
        bid = _number(tick.get("bid"), "bid")
        ask = _number(tick.get("ask"), "ask")
        if bid <= 0 or ask <= bid:
            raise OrderValidationError("Current bid/ask is invalid")
        return tick

    def _filling_mode(self, info: Mapping[str, Any]) -> int:
        """Map the broker's SYMBOL_FILLING_* bitmask to ORDER_FILLING_* constants."""
        mask = int(info.get("filling_mode") or 0)
        if mask & 1:  # SYMBOL_FILLING_FOK
            return int(getattr(self.api, "ORDER_FILLING_FOK", 0))
        if mask & 2:  # SYMBOL_FILLING_IOC
            return int(getattr(self.api, "ORDER_FILLING_IOC", 1))
        if mask & 4:  # SYMBOL_FILLING_BOC
            return int(getattr(self.api, "ORDER_FILLING_BOC", 3))
        return int(getattr(self.api, "ORDER_FILLING_RETURN", 2))

    def calculate_volume(self, symbol: str, entry: float, stop_loss: float, risk_usd: float, info: Mapping[str, Any] | None = None) -> tuple[float, float]:
        info = info or self._symbol_info(symbol)
        point = _number(info.get("point"), "symbol point")
        tick_size = _number(info.get("trade_tick_size") or point, "trade tick size")
        tick_value = _number(info.get("trade_tick_value_loss") or info.get("trade_tick_value"), "trade tick value")
        minimum = _number(info.get("volume_min"), "minimum volume")
        maximum = _number(info.get("volume_max"), "maximum volume")
        step = _number(info.get("volume_step"), "volume step")
        distance = abs(entry - stop_loss)
        if point <= 0 or tick_size <= 0 or tick_value <= 0 or distance <= 0:
            raise OrderValidationError("Cannot calculate risk from symbol specifications")
        risk_per_lot = distance / tick_size * tick_value
        raw_volume = risk_usd / risk_per_lot
        volume = math.floor(raw_volume / step + 1e-12) * step
        volume = round(volume, 8)
        if volume < minimum:
            raise RiskLimitError(f"Minimum broker volume {minimum} would exceed the permitted risk")
        if volume > maximum:
            volume = maximum
        actual_risk = volume * risk_per_lot
        if actual_risk > risk_usd + 1e-8:
            raise RiskLimitError("Rounded broker volume exceeds the permitted risk")
        return volume, actual_risk

    @staticmethod
    def _retcode(value: Any) -> int | None:
        raw = _record_to_dict(value)
        try:
            return int(raw.get("retcode")) if raw.get("retcode") is not None else None
        except (TypeError, ValueError):
            return None

    def submit(self, decision: Mapping[str, Any], max_spread_points: float = 30.0, deviation_points: int = 20) -> dict[str, Any]:
        identity = self.verify_demo_identity()
        direction = str(decision.get("decision") or "NO_TRADE").upper()
        if direction == "NO_TRADE":
            return {"status": "NO_TRADE", "decision": dict(decision), "account": identity["account"]}
        if direction not in {"BUY", "SELL"}:
            raise OrderValidationError("Decision must be BUY, SELL, or NO_TRADE")
        signal_id = str(decision.get("signal_id") or "")
        if not signal_id:
            raise OrderValidationError("A unique signal_id is required")
        if self.store.execution_order(signal_id) is not None:
            raise OrderValidationError(f"Duplicate signal {signal_id} has already been processed")
        if decision.get("stop_loss") is None:
            raise OrderValidationError("Protective stop-loss is required")
        if decision.get("take_profit") is None:
            raise OrderValidationError("Take-profit is required for this experimental adapter")
        permissions = self.verify_execution_permissions()
        positions = self._positions()
        if len(positions) >= self.budget.max_open_positions:
            raise RiskLimitError("Maximum experimental open-position limit reached")
        risk = self.risk_snapshot()
        risk_budget = min(self.budget.max_risk_per_trade_usd, risk["remaining_usd"])
        if risk_budget <= 0:
            raise RiskLimitError("Experimental risk allocation is exhausted")
        symbol = str(decision.get("symbol") or "").upper()
        timeframe = str(decision.get("timeframe") or "").upper()
        entry = _number(decision.get("entry"), "entry")
        stop_loss = _number(decision.get("stop_loss"), "stop-loss")
        take_profit = _number(decision.get("take_profit"), "take-profit")
        info = self._symbol_info(symbol)
        tick = self._tick(symbol)
        point = _number(info.get("point"), "symbol point")
        bid = _number(tick.get("bid"), "bid")
        ask = _number(tick.get("ask"), "ask")
        spread_points = (ask - bid) / point
        if spread_points > max_spread_points:
            raise OrderValidationError(f"Spread {spread_points:.2f} exceeds {max_spread_points:.2f} points")
        current_entry = ask if direction == "BUY" else bid
        if abs(current_entry - entry) > deviation_points * point:
            raise OrderValidationError("Entry price moved beyond the allowed deviation")
        if direction == "BUY" and not stop_loss < entry < take_profit:
            raise OrderValidationError("BUY requires SL < entry < TP")
        if direction == "SELL" and not take_profit < entry < stop_loss:
            raise OrderValidationError("SELL requires TP < entry < SL")
        volume, actual_risk = self.calculate_volume(symbol, entry, stop_loss, risk_budget, info)
        now = datetime.now(timezone.utc).isoformat()
        request = {
            "action": getattr(self.api, "TRADE_ACTION_DEAL", 1),
            "symbol": symbol,
            "volume": volume,
            "type": getattr(self.api, "ORDER_TYPE_BUY" if direction == "BUY" else "ORDER_TYPE_SELL", 0 if direction == "BUY" else 1),
            "price": current_entry,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": deviation_points,
            "magic": 260001,
            "comment": "LT-DEMO",
            "type_time": getattr(self.api, "ORDER_TIME_GTC", 0),
            "type_filling": self._filling_mode(info),
        }
        self.store.save_execution_order({
            "signal_id": signal_id, "status": "PENDING", "symbol": symbol, "timeframe": timeframe,
            "direction": direction, "entry": current_entry, "stop_loss": stop_loss, "take_profit": take_profit,
            "volume": volume, "risk_amount": actual_risk, "request_json": json.dumps(request),
            "result_json": "{}", "created_at": now, "updated_at": now,
        })
        try:
            check = self.api.order_check(request)
            check_raw = _record_to_dict(check)
            check_code = self._retcode(check)
            if check_code not in (None, 0, 10009):
                self.store.update_execution_order(signal_id, status="REJECTED", result_json=json.dumps(check_raw, default=str))
                raise ExecutionError(f"MT5 order_check rejected request: {check_raw}")
            result = self.api.order_send(request)
        except ExecutionError:
            raise
        except Exception as exc:
            self.store.update_execution_order(signal_id, status="UNCERTAIN", result_json=json.dumps({"error": str(exc)}))
            raise ExecutionError(f"Order submission state is uncertain: {exc}") from exc
        result_raw = _record_to_dict(result)
        retcode = self._retcode(result)
        success_codes = {10008, 10009, 10010}
        if retcode not in success_codes:
            self.store.update_execution_order(signal_id, status="REJECTED", result_json=json.dumps(result_raw, default=str))
            raise ExecutionError(f"MT5 rejected order: {result_raw}")
        order_ticket = result_raw.get("order")
        position_ticket = result_raw.get("position")
        broker_positions = self._positions()
        matching = [item for item in broker_positions if str(item.get("symbol", "")).upper() == symbol]
        status = "POSITION_OPEN" if matching else "UNCERTAIN"
        if status == "UNCERTAIN":
            self.store.update_execution_order(signal_id, status=status, order_ticket=order_ticket, position_ticket=position_ticket, result_json=json.dumps(result_raw, default=str))
            raise ExecutionError("Broker accepted the order response but position reconciliation is unresolved")
        position = matching[0]
        broker_sl = position.get("sl")
        if broker_sl is None or abs(float(broker_sl) - stop_loss) > point:
            self.store.update_execution_order(signal_id, status="PROTECTION_UNVERIFIED", order_ticket=order_ticket, position_ticket=position.get("ticket"), result_json=json.dumps(result_raw, default=str))
            raise ExecutionError("Broker position does not confirm the requested protective stop-loss")
        self.store.update_execution_order(signal_id, status=status, order_ticket=order_ticket, position_ticket=position.get("ticket"), result_json=json.dumps(result_raw, default=str))
        return {
            "status": status,
            "signal_id": signal_id,
            "order_ticket": order_ticket,
            "position_ticket": position.get("ticket"),
            "position": position,
            "risk_amount": actual_risk,
            "volume": volume,
            "spread_points": spread_points,
            "account": permissions["account"],
            "result": result_raw,
        }
