from datetime import timedelta

from app.worker.store import JobStore, iso, utc_now


def payload(webhook_id="evt-1", action="open", mr_iid=2):
    return {
        "webhook_id": webhook_id,
        "object_kind": "merge_request",
        "object_attributes": {"iid": mr_iid, "action": action},
        "project": {"id": 4, "path_with_namespace": "group/project"},
    }


def test_retry_wait_respects_backoff_deadline(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-retry", "4", "group/project", 12, "update", "Merge Request Hook", payload("evt-retry", "update", 12))

    claimed = store.claim("worker-a", 30)
    assert claimed is not None
    assert claimed.job_id == job.job_id

    store.fail(job.job_id, "temporary downstream error", 5, 30, 120, permanent=False, retry_classification="transient")
    recovered = store.get(job.job_id)
    assert recovered.status == "RETRY_WAIT"
    assert recovered.next_retry_at is not None

    early_claim = store.claim("worker-b", 30)
    assert early_claim is None

    with store._connect() as connection:
        connection.execute("UPDATE jobs SET next_retry_at = ? WHERE job_id = ?", (iso(utc_now() - timedelta(seconds=5)), job.job_id))

    retry_claim = store.claim("worker-c", 30)
    assert retry_claim is not None
    assert retry_claim.job_id == job.job_id
    assert retry_claim.status == "PROCESSING"


def test_expired_lease_recovery_resets_owner_and_requeues_job(tmp_path):
    store = JobStore(str(tmp_path / "jobs.sqlite3"))
    job, _ = store.enqueue("evt-expire", "4", "group/project", 37, "open", "Merge Request Hook", payload("evt-expire", "open", 37))

    claimed = store.claim("worker-a", 1)
    assert claimed is not None
    assert claimed.job_id == job.job_id

    with store._connect() as connection:
        connection.execute(
            "UPDATE jobs SET lease_until = ?, status = 'PROCESSING', worker_id = 'worker-a' WHERE job_id = ?",
            (iso(utc_now() - timedelta(seconds=10)), job.job_id),
        )

    recovered_count = store.recover_expired()
    assert recovered_count == 1

    recovered_job = store.get(job.job_id)
    assert recovered_job.status == "RETRY_WAIT"
    assert recovered_job.worker_id is None
    assert recovered_job.lease_until is None

    retried = store.claim("worker-b", 60)
    assert retried is not None
    assert retried.job_id == job.job_id
    assert retried.worker_id == "worker-b"
