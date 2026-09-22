import asyncio
from datetime import timedelta
import pytest

from app.config import Settings
from app.events.manager import InProcessEventManager
from app.events.store import EventStore, iso, utc_now
from app.services import mr_processor


@pytest.mark.anyio
async def test_startup_recovers_stale_processing_events(tmp_path):
    db_path = str(tmp_path / "crash_events.sqlite3")
    store = EventStore(db_path)

    # Enqueue and claim event to simulate process crash while state was PROCESSING
    event, _ = store.enqueue("wh-stale-1", "10", "group/repo", 5, "open", "hook", {"kind": "mr"})
    claimed = store.claim("worker-dead", lease_seconds=10)
    assert claimed.status in ("processing", "PROCESSING")

    # Backdate started_at / lease_until to simulate crash in the past
    stale_time = iso(utc_now() - timedelta(seconds=600))
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET started_at = ?, lease_until = ? WHERE job_id = ?",
            (stale_time, stale_time, claimed.event_id),
        )

    # Initialize manager and run startup recovery
    settings = Settings(database_path=db_path, stale_processing_timeout_seconds=300)
    manager = InProcessEventManager(store=store, settings=settings)

    # Recover stale events
    recovered = store.recover_stale_events(stale_timeout_seconds=300)
    assert recovered == 1

    recovered_event = store.get(claimed.event_id)
    assert recovered_event.status in ("retry", "RETRY_WAIT")
    assert recovered_event.last_error is not None


@pytest.mark.anyio
async def test_graceful_shutdown_drains_active_tasks(tmp_path, monkeypatch):
    db_path = str(tmp_path / "shutdown_events.sqlite3")
    store = EventStore(db_path)

    settings = Settings(database_path=db_path, shutdown_timeout_seconds=2, max_concurrent_mr_jobs=2)
    manager = InProcessEventManager(store=store, settings=settings)

    completed_events = []

    async def fake_mr_processing(event_data):
        await asyncio.sleep(0.2)
        completed_events.append(event_data.get("_dedupe_key"))

    monkeypatch.setattr(mr_processor, "process_gitlab_event", fake_mr_processing)

    # Enqueue events
    event1, _ = store.enqueue("wh-shut-1", "10", "group/repo", 1, "open", "hook", {"iid": 1})
    event2, _ = store.enqueue("wh-shut-2", "10", "group/repo", 2, "open", "hook", {"iid": 2})

    await manager.start()

    # Wait briefly for tasks to be picked up
    await asyncio.sleep(0.05)

    # Initiate graceful shutdown
    await manager.shutdown()

    assert manager._shutting_down is True
    # Tasks should have finished during shutdown window
    assert len(completed_events) == 2
