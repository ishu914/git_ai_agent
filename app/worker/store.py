import contextlib
import json
import logging
import os
import random
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.observability import record_metric, set_gauge
from app.worker.models import Job

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 2


JOB_COLUMNS = (
    "job_id, webhook_id, project_id, project_path, mr_iid, action, event_type, payload,"
    " received_at, status, attempt_count, created_at, started_at, completed_at,"
    " next_retry_at, last_error, retry_classification, last_failed_at, review_fingerprint, worker_id, lease_until"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class JobStore:
    """SQLite-backed durable job store for the receiver and worker processes."""

    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        if database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                Path(database_path).parent.chmod(0o700)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        if self.database_path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(f"Unsupported job database schema version: {current_version}")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    webhook_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    project_path TEXT,
                    mr_iid INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    next_retry_at TEXT,
                    last_error TEXT,
                    retry_classification TEXT,
                    last_failed_at TEXT,
                    review_fingerprint TEXT,
                    worker_id TEXT,
                    lease_until TEXT
                )
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
            if "retry_classification" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN retry_classification TEXT")
            if "last_failed_at" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN last_failed_at TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_retry ON jobs(status, next_retry_at)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_jobs_mr_status ON jobs(project_id, mr_iid, status)")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        if self.database_path != ":memory:":
            with contextlib.suppress(OSError):
                Path(self.database_path).chmod(0o600)

    def enqueue(
        self,
        webhook_id: str,
        project_id: str,
        project_path: Optional[str],
        mr_iid: int,
        action: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> tuple[Job, bool]:
        now = utc_now()
        job_id = str(uuid.uuid4())
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE webhook_id = ?",
                (webhook_id,),
            ).fetchone()
            if existing:
                connection.execute("COMMIT")
                return self._row_to_job(existing), False

            # Older pending work for the same MR cannot represent a newer GitLab state.
            connection.execute(
                """
                UPDATE jobs
                SET status = 'OBSOLETE', completed_at = ?
                WHERE project_id = ? AND mr_iid = ? AND status = 'PENDING'
                """,
                (iso(now), project_id, mr_iid),
            )
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, webhook_id, project_id, project_path, mr_iid, action, event_type,
                    payload, received_at, status, attempt_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
                """,
                (
                    job_id,
                    webhook_id,
                    project_id,
                    project_path,
                    mr_iid,
                    action,
                    event_type,
                    payload_text,
                    iso(now),
                    iso(now),
                ),
            )
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            connection.execute("COMMIT")
        record_metric("jobs_created_total")
        set_gauge("pending_jobs", self.status_counts().get("PENDING", 0))
        return self._row_to_job(row), True

    def get(self, job_id: str) -> Optional[Job]:
        with self._connect() as connection:
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def get_by_webhook_id(self, webhook_id: str) -> Optional[Job]:
        with self._connect() as connection:
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE webhook_id = ?", (webhook_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def claim(self, worker_id: str, lease_seconds: int) -> Optional[Job]:
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.recover_expired_locked(connection, now)
            row = connection.execute(
                f"""
                SELECT {JOB_COLUMNS} FROM jobs
                WHERE status IN ('PENDING', 'RETRY_WAIT')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (iso(now),),
            ).fetchone()
            if not row:
                connection.execute("COMMIT")
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'PROCESSING', attempt_count = attempt_count + 1,
                    started_at = ?, worker_id = ?, lease_until = ?, last_error = NULL
                WHERE job_id = ? AND status IN ('PENDING', 'RETRY_WAIT')
                """,
                (iso(now), worker_id, iso(lease_until), row["job_id"]),
            )
            claimed = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            connection.execute("COMMIT")
        record_metric("jobs_claimed_total")
        set_gauge("processing_jobs", self.status_counts().get("PROCESSING", 0))
        return self._row_to_job(claimed)

    def complete(self, job_id: str, review_fingerprint: Optional[str] = None) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'COMPLETED', completed_at = ?, lease_until = NULL,
                    worker_id = NULL, review_fingerprint = ?
                WHERE job_id = ?
                """,
                (iso(now), review_fingerprint, job_id),
            )
        record_metric("jobs_completed_total")
        set_gauge("completed_jobs", self.status_counts().get("COMPLETED", 0))

    def fail(
        self,
        job_id: str,
        error: str,
        max_attempts: int,
        backoff_base: int,
        backoff_max: int,
        permanent: bool = False,
        retry_classification: str = "transient",
    ) -> None:
        now = utc_now()
        with self._connect() as connection:
            row = connection.execute("SELECT attempt_count FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            attempts = int(row["attempt_count"]) if row else max_attempts
            dead_letter = permanent or attempts >= max_attempts
            delay = min(backoff_max, backoff_base * (2 ** max(0, attempts - 1))) + random.uniform(0, 1)
            status = "DEAD_LETTER" if dead_letter else "RETRY_WAIT"
            next_retry = None if dead_letter else iso(now + timedelta(seconds=delay))
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, last_error = ?, retry_classification = ?, last_failed_at = ?,
                    next_retry_at = ?, lease_until = NULL, worker_id = ?
                WHERE job_id = ?
                """,
                (status, error[:1000], retry_classification, iso(now), next_retry, None, job_id),
            )
        if status == "RETRY_WAIT":
            record_metric("jobs_retried_total")
        if dead_letter:
            record_metric("jobs_dead_letter_total")
        set_gauge("retry_wait_jobs", self.status_counts().get("RETRY_WAIT", 0))
        set_gauge("dead_letter_jobs", self.status_counts().get("DEAD_LETTER", 0))

    def renew_lease(self, job_id: str, worker_id: str, lease_seconds: int) -> bool:
        lease_until = utc_now() + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_until = ?
                WHERE job_id = ? AND status = 'PROCESSING' AND worker_id = ?
                """,
                (iso(lease_until), job_id, worker_id),
            )
        return cursor.rowcount == 1

    def recover_expired(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = self.recover_expired_locked(connection, utc_now())
            connection.execute("COMMIT")
        if count:
            record_metric("jobs_recovered_total")
            set_gauge("retry_wait_jobs", self.status_counts().get("RETRY_WAIT", 0))
        return count

    @staticmethod
    def recover_expired_locked(connection: sqlite3.Connection, now: datetime) -> int:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET status = 'RETRY_WAIT', next_retry_at = ?, last_error = 'worker lease expired',
                worker_id = NULL, lease_until = NULL
            WHERE status = 'PROCESSING' AND lease_until IS NOT NULL AND lease_until <= ?
            """,
            (iso(now), iso(now)),
        )
        return cursor.rowcount

    def list_jobs(self, limit: int = 100) -> List[Job]:
        with self._connect() as connection:
            rows = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_job(row) for row in rows]

    def status_counts(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def list_dead_letter(self, limit: int = 100) -> List[Job]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE status = 'DEAD_LETTER' ORDER BY last_failed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def requeue_dead_letter(self, job_id: str) -> Job:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                connection.execute("ROLLBACK")
                raise KeyError(f"Unknown job ID: {job_id}")
            if row["status"] != "DEAD_LETTER":
                connection.execute("ROLLBACK")
                raise ValueError("Only DEAD_LETTER jobs can be requeued")
            connection.execute(
                """
                UPDATE jobs SET status = 'PENDING', attempt_count = 0, next_retry_at = NULL,
                    worker_id = NULL, lease_until = NULL, completed_at = NULL,
                    retry_classification = 'manual_requeue', last_error = 'manually requeued'
                WHERE job_id = ?
                """,
                (job_id,),
            )
            updated = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            connection.execute("COMMIT")
        return self._row_to_job(updated)

    def cleanup_retention(self, retention_days: int) -> int:
        cutoff = utc_now() - timedelta(days=retention_days)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('COMPLETED', 'OBSOLETE', 'DEAD_LETTER')
                  AND COALESCE(completed_at, last_failed_at, created_at) < ?
                """,
                (iso(cutoff),),
            )
            connection.execute("COMMIT")
        record_metric("retention_cleanup_total")
        set_gauge("completed_jobs", self.status_counts().get("COMPLETED", 0))
        return cursor.rowcount

    def backup(self, destination: str) -> None:
        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source = self._connect()
        target = sqlite3.connect(str(destination_path))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        with contextlib.suppress(OSError):
            destination_path.chmod(0o600)

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(**dict(row))
