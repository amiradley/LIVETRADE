from __future__ import annotations

import sqlite3
from typing import Any


PILOT_DATASETS = (
    ("EURUSD", "H1", 1000),
    ("EURUSD", "M15", 2000),
    ("GBPUSD", "H1", 1000),
    ("GBPUSD", "M15", 2000),
    ("AUDUSD", "H1", 1000),
    ("AUDUSD", "M15", 2000),
    ("NZDUSD", "H1", 1000),
    ("NZDUSD", "M15", 2000),
)


def _gate(name: str, status: str, detail: str, evidence: dict[str, Any], required: bool = True) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "evidence": evidence,
        "required": required,
    }


def _dataset_audit(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    datasets: list[dict[str, Any]] = []
    for symbol, timeframe, target in PILOT_DATASETS:
        row = connection.execute(
            """
            SELECT COUNT(*) AS candles,
                   SUM(CASE WHEN quality_status='VALID' THEN 1 ELSE 0 END) AS valid,
                   SUM(CASE WHEN quality_status='VALID_BUT_SHORT' THEN 1 ELSE 0 END) AS short,
                   SUM(CASE WHEN quality_status NOT IN ('VALID','VALID_BUT_SHORT') THEN 1 ELSE 0 END) AS other
            FROM historical_candles WHERE symbol=? AND timeframe=?
            """,
            (symbol, timeframe),
        ).fetchone()
        raw_duplicates = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT raw_time FROM historical_candles
                WHERE symbol=? AND timeframe=? GROUP BY raw_time HAVING COUNT(*) > 1
            )
            """,
            (symbol, timeframe),
        ).fetchone()[0]
        effective_duplicates = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT effective_time FROM historical_candles
                WHERE symbol=? AND timeframe=? GROUP BY effective_time HAVING COUNT(*) > 1
            )
            """,
            (symbol, timeframe),
        ).fetchone()[0]
        job = connection.execute(
            """
            SELECT job_id, downloaded_bars, target_bars, status, attempts, last_error
            FROM collection_jobs WHERE symbol=? AND timeframe=? AND tier='TIER1'
            """,
            (symbol, timeframe),
        ).fetchone()
        candle_count = int(row["candles"] or 0)
        datasets.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "candles": candle_count,
                "target": target,
                "remaining": max(0, target - candle_count),
                "quality": {
                    "valid": int(row["valid"] or 0),
                    "valid_but_short": int(row["short"] or 0),
                    "other": int(row["other"] or 0),
                },
                "raw_duplicate_groups": int(raw_duplicates),
                "effective_duplicate_groups": int(effective_duplicates),
                "job": dict(job) if job is not None else None,
            }
        )
    return datasets


def build_trial_readiness(store: Any) -> dict[str, Any]:
    """Build a conservative, read-only report for the first demo-trial gate."""
    with store.connect() as connection:
        connection.row_factory = sqlite3.Row
        datasets = _dataset_audit(connection)
        strategy_count = int(connection.execute("SELECT COUNT(*) FROM strategy_versions").fetchone()[0])
        demo_eligible_strategies = int(
            connection.execute(
                "SELECT COUNT(*) FROM strategy_versions WHERE approval_status='DEMO_ELIGIBLE' AND status IN ('ACTIVE','APPROVED')"
            ).fetchone()[0]
        )
        validation_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT status, COUNT(*) FROM validation_runs GROUP BY status")
        }
        experiment_counts = {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT status, COUNT(*) FROM research_experiments GROUP BY status")
        }

    qualified = [
        item
        for item in datasets
        if item["candles"] >= item["target"]
        and item["quality"]["other"] == 0
        and item["raw_duplicate_groups"] == 0
        and item["effective_duplicate_groups"] == 0
    ]
    completed_experiments = experiment_counts.get("COMPLETE", 0)
    passing_validations = validation_counts.get("PASS", 0)
    gates = [
        _gate(
            "data",
            "PASS" if qualified else "BLOCKED",
            "At least one pilot dataset meets its approved candle target and structural checks."
            if qualified
            else "No EURUSD/GBPUSD/AUDUSD/NZDUSD H1 or M15 dataset currently meets its approved target.",
            {"qualified_datasets": [f"{item['symbol']} {item['timeframe']}" for item in qualified], "datasets": datasets},
        ),
        _gate(
            "research",
            "PASS" if completed_experiments and passing_validations else ("PARTIAL" if completed_experiments else "BLOCKED"),
            "Completed research has at least one passing validation."
            if completed_experiments and passing_validations
            else "Research evidence exists, but no experiment has produced a passing validation."
            if completed_experiments
            else "No completed research experiment exists.",
            {"experiment_counts": experiment_counts, "validation_counts": validation_counts},
        ),
        _gate(
            "strategy",
            "PASS" if demo_eligible_strategies else "BLOCKED",
            "A deterministic strategy version is explicitly marked DEMO_ELIGIBLE."
            if demo_eligible_strategies
            else "No strategy version is registered or approved for demo testing.",
            {"strategy_versions": strategy_count, "demo_eligible_strategies": demo_eligible_strategies},
        ),
        _gate(
            "validation",
            "PASS" if passing_validations else "BLOCKED",
            "At least one validation run passed."
            if passing_validations
            else "No validation run passed; current results are rejected, inconclusive, failed, or tracking-only.",
            {"validation_counts": validation_counts},
        ),
        _gate(
            "risk_controls",
            "BLOCKED",
            "Operational demo risk limits, spread/slippage gates, duplicate-order prevention, and reconciliation are not implemented.",
            {"execution_enabled": False},
        ),
        _gate(
            "execution",
            "BLOCKED",
            "The MT5 adapter is deliberately read-only and exposes no order mutation path.",
            {"adapter": "ReadOnlyMt5Client", "execution_enabled": False},
        ),
        _gate(
            "stop_recovery",
            "PARTIAL",
            "Research/demo-shadow STOP and unresolved-position reporting are implemented; execution-specific recovery is not.",
            {"research_stop_available": True, "execution_recovery_verified": False},
        ),
        _gate(
            "monitoring",
            "PARTIAL",
            "Dashboard, session records, MT5 snapshots, and logs exist; the five-day execution scorecard is not implemented.",
            {"dashboard": True, "session_records": True, "trial_scorecard": False},
        ),
        _gate(
            "authorization",
            "BLOCKED",
            "The user has not separately authorized a demo execution trial.",
            {"explicit_trial_authorization": False},
        ),
    ]
    mandatory = [gate for gate in gates if gate["required"]]
    overall = "DEMO_ELIGIBLE" if all(gate["status"] == "PASS" for gate in mandatory) else "NOT_READY"
    lifecycle = "DEMO-TESTING" if overall == "DEMO_ELIGIBLE" else "RESEARCH IDEA"
    return {
        "overall_status": overall,
        "lifecycle_stage": lifecycle,
        "execution_enabled": False,
        "trial_spec": {
            "duration_trading_days": 5,
            "capital_rm_equivalent": 100,
            "account_currency": "VERIFY_AT_FINAL_GATE",
            "authorization_required": True,
        },
        "gates": gates,
        "blockers": [gate["detail"] for gate in mandatory if gate["status"] != "PASS"],
        "next_action": "Qualify one pilot dataset and produce a deterministic, cost-aware strategy candidate before any demo authorization.",
        "safety_message": "Research-only report. No orders were placed, modified, or closed.",
    }
