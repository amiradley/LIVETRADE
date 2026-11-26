from datetime import date, datetime, timedelta, timezone

import pytest

from live_trade.intelligence import ExchangeRate
from live_trade.risk import PersistentRiskManager
from live_trade.storage import MarketDataStore


RATE = 4.0  # USD -> MYR, making the approved RM thresholds easy to assert.


def _manager(tmp_path, *, status="OK", retrieved_at=None, broker_offset_seconds=0.0):
    store = MarketDataStore(tmp_path / "risk.sqlite3")
    store.save_exchange_rate(
        ExchangeRate(
            "USD", "MYR", RATE, "test-recorded-rate", retrieved_at or datetime.now(timezone.utc),
            date.today(), status,
        )
    )
    return store, PersistentRiskManager(store, broker_offset_seconds)


def _deal(ticket, when, profit):
    return {
        "ticket": ticket,
        "order": ticket + 1000,
        "position_id": ticket + 2000,
        "symbol": "EURUSD",
        "type": 1,
        "entry": 1,
        "volume": 0.1,
        "price": 1.1,
        "profit": profit,
        "commission": 0.0,
        "swap": 0.0,
        "fee": 0.0,
        "time_msc": int(when.timestamp() * 1000),
    }


def test_approved_limits_convert_from_myr_using_recorded_rate_and_persist(tmp_path):
    store, manager = _manager(tmp_path)
    configuration = manager.ensure_configuration(datetime(2026, 11, 26, tzinfo=timezone.utc))

    assert configuration is not None
    assert configuration.to_dict()["daily_loss_limit_usd"] == 1.22
    assert configuration.to_dict()["weekly_loss_limit_usd"] == 2.4425
    assert configuration.to_dict()["max_drawdown_usd"] == 4.885
    assert store.risk_configuration()["timezone_name"] == "Asia/Kuala_Lumpur"
    assert PersistentRiskManager(store).ensure_configuration().usd_to_myr == RATE


def test_daily_limit_uses_malaysia_midnight_and_deduplicated_deals(tmp_path):
    store, manager = _manager(tmp_path)
    now = datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)  # 10:00 Thursday in Malaysia.
    malaysia_tuesday = datetime(2026, 11, 24, 23, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    malaysia_today = datetime(2026, 11, 26, 9, 0, tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    store.save_broker_deals([_deal(1, malaysia_tuesday, -0.5), _deal(2, malaysia_today, -1.22)], now)
    store.save_broker_deals([_deal(2, malaysia_today, -1.22)], now)

    assessment = manager.assess({"balance": 100.0, "equity": 100.0}, now)

    assert assessment.period["daily_net_usd"] == -1.22
    assert assessment.period["weekly_net_usd"] == -1.72
    assert "daily_realised_loss_limit_reached" in assessment.reasons
    assert assessment.period["daily_deal_count"] == 1
    assert len(store.risk_events()) == 1


def test_drawdown_includes_floating_loss_and_survives_manager_restart(tmp_path):
    store, manager = _manager(tmp_path)
    now = datetime(2026, 11, 26, 2, 0, tzinfo=timezone.utc)
    first = manager.assess({"balance": 100.0, "equity": 100.0}, now)
    second = PersistentRiskManager(store).assess({"balance": 100.0, "equity": 95.115}, now + timedelta(minutes=1))

    assert first.current_equity_usd == 100.0
    assert second.peak_equity_usd == 100.0
    assert second.floating_pnl_usd == pytest.approx(-4.885)
    assert "maximum_drawdown_reached" in second.reasons
    assert store.risk_state()["baseline_equity_usd"] == 100.0


def test_stale_exchange_rate_fails_closed(tmp_path):
    store, manager = _manager(tmp_path, retrieved_at=datetime(2026, 11, 20, tzinfo=timezone.utc))
    assessment = manager.assess({"balance": 100.0, "equity": 100.0}, datetime(2026, 11, 26, tzinfo=timezone.utc))

    assert assessment.entries_allowed is False
    assert "risk_exchange_rate_stale" in assessment.reasons


def test_realised_deal_uses_verified_broker_offset_without_rewriting_raw_time(tmp_path):
    store, manager = _manager(tmp_path, broker_offset_seconds=7200)
    now = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
    raw_broker_label = datetime(2026, 11, 26, 11, 30, tzinfo=timezone.utc)
    store.save_broker_deals([_deal(9, raw_broker_label, -1.22)], now)

    assessment = manager.assess({"balance": 100.0, "equity": 100.0}, now)

    assert assessment.period["daily_net_usd"] == -1.22
    assert assessment.period["daily_deal_count"] == 1
    assert "daily_realised_loss_limit_reached" in assessment.reasons
