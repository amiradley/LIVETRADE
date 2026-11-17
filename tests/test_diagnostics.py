from datetime import datetime, timezone

from live_trade.diagnostics import build_rejection_report
from live_trade.scanner import ScanResult, ScanRun


def test_rejection_report_distinguishes_instruments_and_events():
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    scan = ScanRun(
        started_at=timestamp,
        completed_at=timestamp,
        discovered_count=3,
        results=(
            ScanResult("EURUSD", timestamp, timestamp, 1.1, 1.1001, 10, 0, "EUROPE", True, True),
            ScanResult("GBPUSD", timestamp, timestamp, 1.2, 1.2001, 10, 100, "EUROPE", False, False, reasons=("stale_tick", "spread_above_limit")),
            ScanResult("USDJPY", timestamp, None, None, None, None, None, "EUROPE", False, False, reasons=("tick_unavailable",)),
        ),
    )

    report = build_rejection_report(scan)

    assert report.rejected_instrument_count == 2
    assert report.rejection_event_count == 3
    assert report.rejection_counts == {
        "spread_above_limit": 1,
        "stale_tick": 1,
        "tick_unavailable": 1,
    }
    assert report.filter_pass_percentages["stale_tick"] == 66.67
