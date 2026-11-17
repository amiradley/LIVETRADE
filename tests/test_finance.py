from datetime import datetime, timezone

from live_trade.finance import FinancialDataService
from live_trade.mt5_client import ReadOnlyMt5Client


def test_financial_snapshot_uses_broker_account_and_positions(fake_mt5):
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()

    snapshot = FinancialDataService(client).capture(
        datetime(2026, 1, 1, tzinfo=timezone.utc)
    )

    assert snapshot.account_type == "DEMO"
    assert snapshot.account_currency == "USD"
    assert snapshot.balance == 10000.0
    assert snapshot.equity == 10000.0
    assert snapshot.floating_pnl == 0.0
    assert snapshot.positions == ()
    assert snapshot.data_quality == "OK"
