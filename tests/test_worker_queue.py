import asyncio
import json
import sqlite3
from datetime import timedelta

import pytest

from app.worker.runner import JobWorker
from app.worker.store import JobStore, SCHEMA_VERSION, iso, utc_now


def payload(webhook_id="evt-1", action="open", mr_iid=2):
    return {
        "webhook_id": webhook_id,
        "object_kind": "merge_request",
        "object_attributes": {"iid": mr_iid, "action": action},
        "project": {"id": 4, "path_with_namespace": "group/project"},
    }


def test_enqueue_is_durable_and_duplicate_safe(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    first, created = store.enqueue("evt-1", "4", "group/project", 2, "open", "Merge Request Hook", payload())
    second, duplicate = store.enqueue("evt-1", "4", "group/project", 2, "open", "Merge Request Hook", payload())

    assert created is True
    assert duplicate is False
    assert first.job_id == second.job_id
    assert store.get_by_webhook_id("evt-1").status == "PENDING"


def test_duplicate_remains_deduplicated_after_store_reopen(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    first_store = JobStore(database_path)
    first, created = first_store.enqueue("evt-restart", "4", None, 2, "open", "Merge Request Hook", payload("evt-restart"))
    second_store = JobStore(database_path)
    second, duplicate = second_store.enqueue("evt-restart", "4", None, 2, "open", "Merge Request Hook", payload("evt-restart"))

    assert created is True
    assert duplicate is False
    assert second.job_id == first.job_id


def test_burst_is_persisted_and_same_mr_is_coalesced(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    for index in range(100):
        store.enqueue(
            f"evt-{index}",
            "4",
            "group/project",
            index % 20,
            "update",
            "Merge Request Hook",
            payload(f"evt-{index}", "update", index % 20),
        )

    jobs = store.list_jobs(200)
    assert len(jobs) == 100
    assert sum(job.status == "PENDING" for job in jobs) == 20
    assert sum(job.status == "OBSOLETE" for job in jobs) == 80


def test_pending_same_mr_is_coalesced(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    first, _ = store.enqueue("evt-a", "4", "group/project", 2, "update", "Merge Request Hook", payload("evt-a", "update"))
    second, _ = store.enqueue("evt-b", "4", "group/project", 2, "update", "Merge Request Hook", payload("evt-b", "update"))

    assert store.get(first.job_id).status == "OBSOLETE"
    assert store.get(second.job_id).status == "PENDING"


def test_claim_is_atomic_and_completes(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-1", "4", None, 2, "open", "Merge Request Hook", payload())
    claimed_one = store.claim("worker-a", 900)
    claimed_two = store.claim("worker-b", 900)

    assert claimed_one.job_id == job.job_id
    assert claimed_two is None
    store.complete(job.job_id, "fingerprint")
    assert store.get(job.job_id).status == "COMPLETED"


def test_expired_lease_is_recoverable(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-1", "4", None, 2, "open", "Merge Request Hook", payload())
    claimed = store.claim("worker-a", 30)
    with store._connect() as connection:
        connection.execute("UPDATE jobs SET lease_until = '2000-01-01T00:00:00+00:00' WHERE job_id = ?", (job.job_id,))
    assert store.recover_expired() == 1
    recovered = store.claim("worker-b", 900)
    assert recovered.job_id == claimed.job_id


def test_worker_success_and_failure_are_persisted(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-1", "4", None, 2, "open", "Merge Request Hook", payload())
    monkeypatch.setattr("app.worker.runner.process_gitlab_event", lambda event: asyncio.sleep(0))
    worker = JobWorker(store=store, worker_id="worker-a")
    result = asyncio.run(worker.run_once())
    assert result.status == "COMPLETED"

    failed_job, _ = store.enqueue("evt-2", "4", None, 3, "open", "Merge Request Hook", payload("evt-2", "open", 3))

    async def fail(event):
        raise RuntimeError("temporary GitLab failure")

    monkeypatch.setattr("app.worker.runner.process_gitlab_event", fail)
    result = asyncio.run(worker.run_once())
    assert result.status == "RETRY_WAIT"
    assert store.get(failed_job.job_id).attempt_count == 1


def test_permanent_failure_dead_letters_after_one_configured_attempt(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-1", "4", None, 2, "open", "Merge Request Hook", payload())
    worker = JobWorker(store=store, worker_id="worker-a")
    worker.settings.worker_max_attempts = 1

    async def fail(event):
        raise RuntimeError("GitLab API 401 unauthorized")

    monkeypatch.setattr("app.worker.runner.process_gitlab_event", fail)
    result = asyncio.run(worker.run_once())
    assert result.status == "DEAD_LETTER"


def test_schema_version_is_recorded_and_newer_schema_is_rejected(tmp_path):
    database_path = str(tmp_path / "jobs.sqlite3")
    store = JobStore(database_path)
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute("PRAGMA user_version = 999")
    assert version == SCHEMA_VERSION
    with pytest.raises(RuntimeError, match="Unsupported job database schema"):
        JobStore(database_path)


def test_retention_removes_only_old_historical_jobs(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    completed, _ = store.enqueue("evt-complete", "4", None, 2, "open", "hook", payload("evt-complete"))
    pending, _ = store.enqueue("evt-pending", "4", None, 3, "open", "hook", payload("evt-pending", mr_iid=3))
    with store._connect() as connection:
        old = iso(utc_now() - timedelta(days=100))
        connection.execute("UPDATE jobs SET status = 'COMPLETED', completed_at = ? WHERE job_id = ?", (old, completed.job_id))
        connection.execute("UPDATE jobs SET created_at = ? WHERE job_id = ?", (old, pending.job_id))
    assert store.cleanup_retention(90) == 1
    assert store.get(completed.job_id) is None
    assert store.get(pending.job_id) is not None


def test_dead_letter_requeue_is_restricted_and_preserves_job(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-1", "4", None, 2, "open", "hook", payload())
    store.claim("worker", 900)
    store.fail(job.job_id, "permanent", 1, 1, 1, permanent=True)
    requeued = store.requeue_dead_letter(job.job_id)
    assert requeued.job_id == job.job_id
    assert requeued.status == "PENDING"
    assert store.get_by_webhook_id("evt-1").job_id == job.job_id
    with pytest.raises(ValueError):
        store.requeue_dead_letter(job.job_id)


def test_online_backup_is_openable(tmp_path):
    source_path = str(tmp_path / "jobs.sqlite3")
    backup_path = str(tmp_path / "backup" / "jobs.sqlite3")
    store = JobStore(source_path)
    store.enqueue("evt-1", "4", None, 2, "open", "hook", payload())
    store.backup(backup_path)
    backup = JobStore(backup_path)
    assert backup.get_by_webhook_id("evt-1") is not None


def test_lease_renewal_requires_owner(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-1", "4", None, 2, "open", "hook", payload())
    store.claim("worker-a", 900)
    assert store.renew_lease(job.job_id, "worker-b", 900) is False
    assert store.renew_lease(job.job_id, "worker-a", 900) is True


def test_job_payload_does_not_retain_unnecessary_mr_content(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    event = payload()
    event["description"] = "should not be persisted"
    job, _ = store.enqueue("evt-minimal", "4", None, 2, "open", "hook", {"project": event["project"], "object_attributes": event["object_attributes"]})

    assert "should not be persisted" not in job.payload
