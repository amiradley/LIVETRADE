import threading
from datetime import datetime

from live_trade.monitoring import DemoMonitoringWorker


def test_monitoring_worker_is_single_instance_and_restartable():
    calls = []
    first_cycle = threading.Event()

    def cycle():
        calls.append(len(calls) + 1)
        first_cycle.set()
        return {"status": "OK"}

    worker = DemoMonitoringWorker(cycle, interval_seconds=1)
    first = worker.start()
    assert first.state in {"STARTING", "RUNNING"}
    assert first_cycle.wait(2)

    duplicate = worker.start()
    assert duplicate.thread_alive is True
    assert worker.health().cycles == 1

    stopped = worker.stop()
    assert stopped.state == "STOPPED"
    assert stopped.thread_alive is False

    second_cycle = threading.Event()

    def cycle_after_restart():
        calls.append(len(calls) + 1)
        second_cycle.set()
        return {"status": "RESTARTED"}

    worker.cycle = cycle_after_restart
    restarted = worker.start()
    assert restarted.thread_alive is True
    assert second_cycle.wait(2)
    assert worker.health().cycles == 2
    worker.stop()


def test_monitoring_worker_records_transient_errors_and_keeps_running():
    attempts = []
    attempted = threading.Event()

    def cycle():
        attempts.append(True)
        attempted.set()
        raise RuntimeError("temporary MT5 failure")

    worker = DemoMonitoringWorker(cycle, interval_seconds=1)
    worker.start()
    assert attempted.wait(2)
    health = worker.health()
    assert health.state == "RUNNING"
    assert health.cycles == 0
    assert health.last_error == "temporary MT5 failure"
    worker.stop()


def test_monitoring_worker_runs_consecutive_interval_cycles():
    completed = threading.Event()
    timestamps = []

    def cycle():
        timestamps.append(datetime.now())
        if len(timestamps) >= 2:
            completed.set()
        return {"status": "NO_TRADE", "fresh_data": True}

    worker = DemoMonitoringWorker(cycle, interval_seconds=1)
    worker.start()
    assert completed.wait(3.5)
    worker.stop()

    assert len(timestamps) >= 2
    assert (timestamps[1] - timestamps[0]).total_seconds() >= 0.9
    assert worker.health().cycles >= 2


def test_monitoring_worker_recovers_from_mt5_and_database_failures():
    completed = threading.Event()
    attempts = []

    def cycle():
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise RuntimeError("MT5 disconnected")
        if len(attempts) == 2:
            raise RuntimeError("database failure")
        completed.set()
        return {"status": "NO_TRADE"}

    worker = DemoMonitoringWorker(cycle, interval_seconds=1)
    worker.start()
    assert completed.wait(4.5)
    health = worker.health()
    worker.stop()

    assert len(attempts) >= 3
    assert health.cycles >= 1
    assert health.last_error is None
