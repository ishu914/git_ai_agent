import json
import sqlite3
from datetime import datetime, timedelta, timezone
import pytest

from app.events.store import EventRecord, EventStore, iso, utc_now
from app.events.errors import classify_error, sanitize_error_message


def test_event_store_initialization_and_pragma_wal(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    with store._connect() as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout >= 30000
    assert foreign_keys == 1
    assert user_version >= 2


def test_enqueue_persists_event_before_acknowledgment(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    payload = {"object_kind": "merge_request", "project": {"id": 10}, "object_attributes": {"iid": 5, "action": "open"}}
    event, created = store.enqueue(
        webhook_id="wh-100",
        project_id="10",
        project_path="group/repo",
        mr_iid=5,
        action="open",
        event_type="Merge Request Hook",
        payload=payload,
    )

    assert created is True
    assert event.dedupe_key == "wh-100"
    assert event.status in ("pending", "PENDING")
    assert event.attempt_count == 0

    fetched = store.get(event.event_id)
    assert fetched is not None
    assert fetched.project_id == "10"
    assert fetched.mr_iid == 5


def test_deduplication_prevents_duplicate_work(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    payload = {"object_kind": "merge_request", "project": {"id": 10}, "object_attributes": {"iid": 5, "action": "open"}}
    event1, created1 = store.enqueue("wh-dup-1", "10", "group/repo", 5, "open", "Merge Request Hook", payload)
    event2, created2 = store.enqueue("wh-dup-1", "10", "group/repo", 5, "open", "Merge Request Hook", payload)

    assert created1 is True
    assert created2 is False
    assert event1.event_id == event2.event_id


def test_secrets_are_redacted_from_stored_payload(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    sensitive_payload = {
        "object_kind": "merge_request",
        "gitlab_token": "glpat-secret1234567890123456",
        "private_key": "sk-secret9999",
        "normal_field": "public_data",
    }
    event, _ = store.enqueue("wh-secret-1", "10", "group/repo", 5, "open", "hook", sensitive_payload)
    parsed = json.loads(event.payload)

    assert parsed["gitlab_token"] == "[REDACTED]"
    assert parsed["private_key"] == "[REDACTED]"
    assert parsed["normal_field"] == "public_data"


def test_event_state_lifecycle_transitions(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    payload = {"object_kind": "merge_request"}
    event, _ = store.enqueue("wh-life-1", "10", "group/repo", 5, "open", "hook", payload)
    assert event.status in ("pending", "PENDING")

    claimed = store.claim("worker-1", lease_seconds=60)
    assert claimed is not None
    assert claimed.event_id == event.event_id
    assert claimed.status in ("processing", "PROCESSING")
    assert claimed.attempt_count == 1

    store.complete(event.event_id, review_fingerprint="fingerprint-abc")
    completed = store.get(event.event_id)
    assert completed.status in ("completed", "COMPLETED")
    assert completed.review_fingerprint == "fingerprint-abc"
    assert completed.completed_at is not None


def test_transient_failure_triggers_retry_wait_with_backoff(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    event, _ = store.enqueue("wh-retry-1", "10", "group/repo", 5, "open", "hook", {})
    claimed = store.claim("worker-1", lease_seconds=60)

    error = RuntimeError("503 Service Unavailable timeout")
    status_result = store.fail(claimed.event_id, error, max_attempts=3, backoff_base=5, backoff_max=300)

    assert status_result in ("retry", "RETRY_WAIT")
    failed = store.get(claimed.event_id)
    assert failed.status in ("retry", "RETRY_WAIT")
    assert failed.next_retry_at is not None
    assert "503" in failed.last_error


def test_permanent_failure_dead_letters_immediately(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    event, _ = store.enqueue("wh-perm-1", "10", "group/repo", 5, "open", "hook", {})
    claimed = store.claim("worker-1", lease_seconds=60)

    error = ValueError("401 Unauthorized token")
    status_result = store.fail(claimed.event_id, error, max_attempts=3, permanent=True)

    assert status_result in ("failed", "DEAD_LETTER")
    failed = store.get(claimed.event_id)
    assert failed.status in ("failed", "DEAD_LETTER")
    assert failed.next_retry_at is None


def test_retention_cleanup_only_deletes_completed_or_failed(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    store = EventStore(db_path)

    evt1, _ = store.enqueue("wh-old-1", "10", "group/repo", 1, "open", "hook", {})
    evt2, _ = store.enqueue("wh-old-2", "10", "group/repo", 2, "open", "hook", {})
    evt3, _ = store.enqueue("wh-active-3", "10", "group/repo", 3, "open", "hook", {})

    store.complete(evt1.event_id)
    store.fail(evt2.event_id, "permanent error", permanent=True)

    # Set old timestamp
    old_time = iso(utc_now() - timedelta(days=100))
    with store._connect() as conn:
        conn.execute("UPDATE jobs SET completed_at = ? WHERE job_id = ?", (old_time, evt1.event_id))
        conn.execute("UPDATE jobs SET last_failed_at = ? WHERE job_id = ?", (old_time, evt2.event_id))

    removed = store.cleanup_retention(retention_days=90)
    assert removed == 2

    assert store.get(evt1.event_id) is None
    assert store.get(evt2.event_id) is None
    assert store.get(evt3.event_id) is not None  # Pending event kept


def test_sqlite_backup_and_restore(tmp_path):
    db_path = str(tmp_path / "events.sqlite3")
    backup_path = str(tmp_path / "backup.sqlite3")
    store = EventStore(db_path)

    store.enqueue("wh-bk-1", "10", "group/repo", 1, "open", "hook", {"data": "test"})
    store.backup(backup_path)

    assert (tmp_path / "backup.sqlite3").exists()
    restored_store = EventStore(backup_path)
    assert restored_store.get_by_dedupe_key("wh-bk-1") is not None
