from datetime import datetime, timezone

from pytest import approx

from live_trade.accounting import aggregate_closed_trades, savings_preview
from live_trade.intelligence import ExchangeRateService
from live_trade.mt5_client import ReadOnlyMt5Client
from live_trade.storage import MarketDataStore


def deal(ticket, entry, volume, price, profit, commission=-1.0, swap=-0.1, fee=-0.2):
    return {
        "ticket": ticket,
        "order": ticket + 100,
        "position_id": 99,
        "symbol": "EURUSD",
        "type": 0 if entry == 0 else 1,
        "entry": entry,
        "volume": volume,
        "price": price,
        "profit": profit,
        "commission": commission,
        "swap": swap,
        "fee": fee,
        "time": int(datetime(2026, 1, 1, ticket - 1, tzinfo=timezone.utc).timestamp()),
    }


def test_partial_closes_are_aggregated_with_all_costs():
    trades = aggregate_closed_trades(
        [deal(1, 0, 1.0, 1.1000, 0.0), deal(2, 1, 0.4, 1.1100, 40.0), deal(3, 1, 0.6, 1.1200, 60.0)]
    )

    assert len(trades) == 1
    assert trades[0].volume == 1.0
    assert trades[0].entry_price == 1.1
    assert trades[0].exit_price == 1.12
    assert trades[0].gross_profit == 100.0
    assert trades[0].commission == -3.0
    assert trades[0].swap == approx(-0.3)
    assert trades[0].fees == approx(-0.6)
    assert trades[0].net_profit == approx(96.1)


def test_savings_preview_requires_recovery_above_high_water_mark():
    first = savings_preview(100.0, 0.0)
    after_loss = savings_preview(80.0, 100.0)
    recovered = savings_preview(120.0, 100.0)

    assert first["savings_amount"] == 20.0
    assert after_loss["eligible_profit"] == 0.0
    assert recovered["eligible_profit"] == 20.0
    assert recovered["savings_amount"] == 4.0


def test_broker_deals_are_deduplicated_and_high_water_mark_persists(tmp_path):
    store = MarketDataStore(tmp_path / "accounting.sqlite3")
    captured = datetime(2026, 1, 3, tzinfo=timezone.utc)
    deals = [deal(1, 0, 1.0, 1.1, 0.0), deal(2, 1, 1.0, 1.11, 100.0)]

    assert store.save_broker_deals(deals, captured) == 2
    assert store.save_broker_deals(deals, captured) == 2
    first = store.apply_savings_allocation(100.0, 2, captured)
    loss = store.apply_savings_allocation(80.0, 2, captured)
    recovered = store.apply_savings_allocation(120.0, 2, captured)

    with store.connect() as connection:
        assert connection.execute("select count(*) from broker_deals").fetchone()[0] == 2
        assert connection.execute("select count(*) from savings_allocations").fetchone()[0] == 2
    assert first["savings_amount"] == 20.0
    assert loss["status"] == "NO_NEW_ELIGIBLE_PROFIT"
    assert recovered["savings_amount"] == 4.0


def test_exchange_rate_failure_is_explicit_and_client_history_is_read_only(tmp_path, fake_mt5, monkeypatch):
    monkeypatch.setenv("LIVE_TRADE_FX_URL", "http://127.0.0.1:1/unavailable")
    rate = ExchangeRateService(MarketDataStore(tmp_path / "fx.sqlite3")).get_rate("USD", "MYR")
    assert rate.rate is None
    assert rate.status == "UNAVAILABLE"

    fake_mt5.history_deals_get = lambda date_from, date_to: (deal(1, 0, 1.0, 1.1, 0.0),)
    client = ReadOnlyMt5Client(api=fake_mt5)
    client.connect()
    assert len(client.history_deals(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc))) == 1
    assert not hasattr(client, "order_send")
