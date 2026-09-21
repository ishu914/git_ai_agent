import shutil
import sqlite3
import threading
import time

import pytest

from app.worker.store import JobStore, SCHEMA_VERSION, iso, utc_now


def make_payload(webhook_id: str = "evt-1", mr_iid: int = 2, action: str = "open"):
    return {
        "webhook_id": webhook_id,
        "object_kind": "merge_request",
        "object_attributes": {"iid": mr_iid, "action": action},
        "project": {"id": 4, "path_with_namespace": "group/project"},
    }


def test_sqlite_database_initialization_contract(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    JobStore(database_path)

    with JobStore(database_path)._connect() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.upper() == "WAL"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert int(connection.execute("PRAGMA busy_timeout").fetchone()[0]) == 30000
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION

        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "jobs" in tables
        assert "idx_jobs_status_retry" in indexes
        assert "idx_jobs_mr_status" in indexes


def test_transaction_failure_rolls_back_without_partial_commit(tmp_path, monkeypatch):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)
    real_connect = sqlite3.connect

    class ExplodingConnection:
        def __init__(self, *args, **kwargs):
            self._inner = real_connect(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def execute(self, sql, *params):
            if sql.strip().startswith("INSERT INTO jobs"):
                raise RuntimeError("forced rollback")
            return self._inner.execute(sql, *params)

    monkeypatch.setattr("app.worker.store.sqlite3.connect", lambda *args, **kwargs: ExplodingConnection(*args, **kwargs))

    with pytest.raises(RuntimeError, match="forced rollback"):
        store.enqueue("evt-fail", "4", "group/project", 7, "open", "hook", make_payload("evt-fail", 7))

    assert store.status_counts() == {}


def test_concurrent_claim_of_same_job_has_single_winner(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)
    job, _ = store.enqueue("evt-shared", "4", "group/project", 8, "open", "hook", make_payload("evt-shared", 8))

    barrier = threading.Barrier(2)
    results = []

    def worker(worker_name: str):
        local = JobStore(database_path)
        barrier.wait()
        results.append(local.claim(worker_name, 300))

    threads = [threading.Thread(target=worker, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [item for item in results if item is not None]
    assert len(winners) == 1
    assert winners[0].job_id == job.job_id


def test_concurrent_claim_of_multiple_jobs_stays_unique(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)
    created = []
    for index in range(5):
        job, _ = store.enqueue(f"evt-{index}", "4", "group/project", index, "open", "hook", make_payload(f"evt-{index}", index))
        created.append(job)

    barrier = threading.Barrier(3)
    results = []

    def worker(worker_name: str):
        local = JobStore(database_path)
        barrier.wait()
        claimed = local.claim(worker_name, 300)
        if claimed is not None:
            results.append(claimed.job_id)

    threads = [threading.Thread(target=worker, args=(f"worker-{index}",)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 3
    assert len(set(results)) == 3


def test_busy_contention_does_not_corrupt_queue(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)

    def hold_lock():
        blocker = sqlite3.connect(database_path)
        blocker.execute("BEGIN IMMEDIATE")
        time.sleep(0.25)
        blocker.commit()
        blocker.close()

    timer = threading.Timer(0.05, hold_lock)
    timer.start()
    job, created = store.enqueue("evt-contention", "4", "group/project", 10, "open", "hook", make_payload("evt-contention", 10))
    timer.join()

    assert created is True
    assert store.get_by_webhook_id("evt-contention").job_id == job.job_id


def test_unavailable_database_path_fails_cleanly(tmp_path):
    database_path = str(tmp_path / "not-a-db")
    (tmp_path / "not-a-db").mkdir()
    with pytest.raises((sqlite3.DatabaseError, OSError)):
        JobStore(database_path)


def test_backup_creates_consistent_snapshot_and_integrity_check_passes(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    backup_path = str(tmp_path / "backup" / "jobs.sqlite3")
    store = JobStore(database_path)

    pending, _ = store.enqueue("evt-pending", "4", "group/project", 9, "open", "hook", make_payload("evt-pending", 9))
    processing = store.claim("worker-a", 300)
    store.fail(processing.job_id, "transient failure", 3, 1, 5)
    completed, _ = store.enqueue("evt-complete", "4", "group/project", 11, "open", "hook", make_payload("evt-complete", 11))
    store.complete(completed.job_id, "fingerprint")
    dead, _ = store.enqueue("evt-dead", "4", "group/project", 12, "open", "hook", make_payload("evt-dead", 12))
    store.fail(dead.job_id, "permanent failure", 1, 1, 5, permanent=True)

    store.backup(backup_path)
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] >= 3

    backup_store = JobStore(backup_path)
    assert backup_store.get_by_webhook_id("evt-pending") is not None
    assert backup_store.get_by_webhook_id("evt-complete") is not None
    assert backup_store.get_by_webhook_id("evt-dead") is not None


def test_backup_during_database_activity_is_openable(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    backup_path = str(tmp_path / "backup" / "jobs.sqlite3")
    store = JobStore(database_path)
    stop_event = threading.Event()

    def writer():
        index = 0
        while not stop_event.is_set():
            store.enqueue(f"evt-live-{index}", "4", "group/project", index, "open", "hook", make_payload(f"evt-live-{index}", index))
            index += 1
            time.sleep(0.01)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    time.sleep(0.05)
    store.backup(backup_path)
    stop_event.set()
    writer_thread.join()

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_failure_is_explicit_and_does_not_corrupt_db(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)
    bad_parent = tmp_path / "bad-parent"
    bad_parent.write_text("not-a-directory", encoding="utf-8")
    bad_destination = bad_parent / "nested" / "jobs.sqlite3"

    with pytest.raises((OSError, FileExistsError, sqlite3.DatabaseError)):
        store.backup(str(bad_destination))

    assert store.get_by_webhook_id("evt-later") is None


def test_restore_to_separate_database_preserves_queue_state(tmp_path):
    database_path = str(tmp_path / "source.sqlite3")
    backup_path = str(tmp_path / "backup" / "jobs.sqlite3")
    restore_path = str(tmp_path / "restore" / "jobs.sqlite3")
    source_store = JobStore(database_path)

    pending, _ = source_store.enqueue("evt-restore-1", "4", "group/project", 13, "open", "hook", make_payload("evt-restore-1", 13))
    processing = source_store.claim("worker-a", 300)
    source_store.fail(processing.job_id, "transient failure", 3, 1, 5)
    completed, _ = source_store.enqueue("evt-restore-2", "4", "group/project", 14, "open", "hook", make_payload("evt-restore-2", 14))
    source_store.complete(completed.job_id, "fingerprint")

    source_store.backup(backup_path)
    restore_path_obj = tmp_path / "restore"
    restore_path_obj.mkdir()
    shutil.copy2(backup_path, restore_path)

    restored = JobStore(restore_path)
    assert restored.get_by_webhook_id("evt-restore-1") is not None
    assert restored.get_by_webhook_id("evt-restore-2") is not None
    with sqlite3.connect(restore_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_retention_only_deletes_historical_jobs_and_keeps_actives(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)
    old_completed, _ = store.enqueue("evt-old-completed", "4", "group/project", 15, "open", "hook", make_payload("evt-old-completed", 15))
    old_dead, _ = store.enqueue("evt-old-dead", "4", "group/project", 16, "open", "hook", make_payload("evt-old-dead", 16))
    active_pending, _ = store.enqueue("evt-active-pending", "4", "group/project", 17, "open", "hook", make_payload("evt-active-pending", 17))
    active_retry, _ = store.enqueue("evt-active-retry", "4", "group/project", 18, "open", "hook", make_payload("evt-active-retry", 18))
    processing_job, _ = store.enqueue("evt-active-processing", "4", "group/project", 19, "open", "hook", make_payload("evt-active-processing", 19))
    store.fail(active_retry.job_id, "temporary failure", 3, 1, 5)

    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'PROCESSING', worker_id = 'worker-a', lease_until = ?, started_at = ? WHERE job_id = ?",
            (iso(utc_now() + __import__("datetime").timedelta(minutes=5)), iso(utc_now()), processing_job.job_id),
        )
        connection.execute(
            "UPDATE jobs SET status = 'COMPLETED', completed_at = ?, last_error = NULL WHERE job_id = ?",
            (iso(utc_now() - __import__("datetime").timedelta(days=120)), old_completed.job_id),
        )
        connection.execute(
            "UPDATE jobs SET status = 'DEAD_LETTER', last_failed_at = ?, last_error = 'stale' WHERE job_id = ?",
            (iso(utc_now() - __import__("datetime").timedelta(days=120)), old_dead.job_id),
        )

    removed = store.cleanup_retention(90)
    assert removed >= 2
    assert store.get(old_completed.job_id) is None
    assert store.get(old_dead.job_id) is None
    assert store.get(active_pending.job_id) is not None
    assert store.get(active_retry.job_id) is not None
    assert store.get(processing_job.job_id) is not None


def test_backup_snapshot_remains_valid_after_retention(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    backup_path = str(tmp_path / "backup" / "jobs.sqlite3")
    store = JobStore(database_path)
    historical, _ = store.enqueue("evt-historical", "4", "group/project", 19, "open", "hook", make_payload("evt-historical", 19))
    active, _ = store.enqueue("evt-active", "4", "group/project", 20, "open", "hook", make_payload("evt-active", 20))

    store.backup(backup_path)
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET status = 'COMPLETED', completed_at = ? WHERE job_id = ?", (iso(utc_now() - __import__("datetime").timedelta(days=200)), historical.job_id))
    store.cleanup_retention(90)

    backup_store = JobStore(backup_path)
    assert backup_store.get(historical.job_id) is not None
    assert store.get(historical.job_id) is None
    assert store.get(active.job_id) is not None
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
