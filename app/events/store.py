import contextlib
import json
import logging
import os
import random
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.events.errors import classify_error, sanitize_error_message
from app.observability import record_metric, set_gauge

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 3


class QueueFullError(RuntimeError):
    """Raised when accepting another queued event would exceed the backlog cap."""

JOB_COLUMNS = (
    "job_id, webhook_id, project_id, project_path, mr_iid, action, event_type, payload, "
    "received_at, status, attempt_count, created_at, started_at, completed_at, "
    "next_retry_at, last_error, retry_classification, last_failed_at, review_fingerprint, worker_id, lease_until"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class EventRecord:
    job_id: str
    webhook_id: str
    project_id: str
    project_path: Optional[str]
    mr_iid: int
    action: str
    event_type: str
    payload: str
    received_at: str
    status: str
    attempt_count: int
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    retry_classification: Optional[str] = None
    last_failed_at: Optional[str] = None
    review_fingerprint: Optional[str] = None
    worker_id: Optional[str] = None
    lease_until: Optional[str] = None

    # Aliases
    @property
    def event_id(self) -> str:
        return self.job_id

    @property
    def dedupe_key(self) -> str:
        return self.webhook_id

    @property
    def error_category(self) -> Optional[str]:
        return self.retry_classification


def sanitize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Remove authorization tokens or secrets from stored event payloads."""
    cleaned = dict(payload)
    for key in list(cleaned.keys()):
        lower_key = str(key).lower()
        if any(tok in lower_key for tok in ["token", "secret", "password", "authorization", "key", "pat"]):
            cleaned[key] = "[REDACTED]"
    return cleaned


class EventStore:
    """SQLite-backed durable event/job store inside the application process."""

    def __init__(self, database_path: str = "data/events.sqlite3") -> None:
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
        webhook_id: Optional[str] = None,
        project_id: str = "",
        project_path: Optional[str] = None,
        mr_iid: int = 0,
        action: str = "updated",
        event_type: str = "merge_request",
        payload: Optional[Dict[str, Any]] = None,
        dedupe_key: Optional[str] = None,
        max_pending: Optional[int] = None,
    ) -> Tuple[EventRecord, bool]:
        """Atomically persist event before acknowledging webhook."""
        key = dedupe_key or webhook_id or str(uuid.uuid4())
        payload_data = payload or {}
        now = utc_now()
        job_id = str(uuid.uuid4())
        safe_payload = sanitize_payload(payload_data)
        payload_text = json.dumps(safe_payload, separators=(",", ":"), sort_keys=True)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE webhook_id = ?",
                (key,),
            ).fetchone()
            if existing:
                connection.execute("COMMIT")
                return self._row_to_event(existing), False

            if max_pending is not None:
                queued = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status IN ('PENDING', 'RETRY_WAIT', 'pending', 'retry')"
                ).fetchone()[0]
                if int(queued) >= max_pending:
                    connection.execute("ROLLBACK")
                    raise QueueFullError(f"Durable event queue reached its configured limit ({max_pending})")

            # Mark older pending/retry work for the same MR as obsolete
            connection.execute(
                """
                UPDATE jobs
                SET status = 'OBSOLETE', completed_at = ?
                WHERE project_id = ? AND mr_iid = ? AND status IN ('PENDING', 'RETRY_WAIT', 'pending', 'retry')
                """,
                (iso(now), str(project_id), int(mr_iid)),
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
                    key,
                    str(project_id),
                    project_path,
                    int(mr_iid),
                    action,
                    event_type,
                    payload_text,
                    iso(now),
                    iso(now),
                ),
            )
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            connection.execute("COMMIT")

        record_metric("events_persisted_total")
        record_metric("jobs_created_total")
        set_gauge("pending_jobs", self.status_counts().get("PENDING", 0))
        return self._row_to_event(row), True

    def enqueue_event(self, *args: Any, **kwargs: Any) -> Tuple[EventRecord, bool]:
        return self.enqueue(*args, **kwargs)

    def get(self, job_id: str) -> Optional[EventRecord]:
        with self._connect() as connection:
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_event(row) if row else None

    def get_by_webhook_id(self, webhook_id: str) -> Optional[EventRecord]:
        with self._connect() as connection:
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE webhook_id = ?", (webhook_id,)).fetchone()
        return self._row_to_event(row) if row else None

    def get_by_dedupe_key(self, dedupe_key: str) -> Optional[EventRecord]:
        return self.get_by_webhook_id(dedupe_key)

    def claim(self, worker_id: str = "in_process", lease_seconds: int = 300) -> Optional[EventRecord]:
        now = utc_now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.recover_expired_locked(connection, now)
            row = connection.execute(
                f"""
                SELECT {JOB_COLUMNS} FROM jobs
                WHERE status IN ('PENDING', 'RETRY_WAIT', 'pending', 'retry')
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
                WHERE job_id = ? AND status IN ('PENDING', 'RETRY_WAIT', 'pending', 'retry')
                """,
                (iso(now), worker_id, iso(lease_until), row["job_id"]),
            )
            claimed = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
            connection.execute("COMMIT")

        record_metric("jobs_claimed_total")
        record_metric("events_claimed_total")
        set_gauge("processing_jobs", self.status_counts().get("PROCESSING", 0))
        return self._row_to_event(claimed)

    def claim_next(self, lease_seconds: int = 300) -> Optional[EventRecord]:
        return self.claim("in_process", lease_seconds)

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
        record_metric("events_completed_total")
        set_gauge("completed_jobs", self.status_counts().get("COMPLETED", 0))

    def fail(
        self,
        job_id: str,
        error: Exception | str,
        max_attempts: int = 3,
        backoff_base: int = 5,
        backoff_max: int = 300,
        permanent: bool = False,
        retry_classification: Optional[str] = None,
    ) -> str:
        now = utc_now()
        if isinstance(error, Exception):
            cat, is_perm = classify_error(error)
            error_text = str(error)
            if retry_classification is None:
                retry_classification = cat
            if permanent is False and is_perm:
                permanent = True
        else:
            error_text = str(error)
            if retry_classification is None:
                retry_classification = "transient"

        sanitized = sanitize_error_message(error_text)

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
                    next_retry_at = ?, lease_until = NULL, worker_id = NULL
                WHERE job_id = ?
                """,
                (status, sanitized, retry_classification, iso(now), next_retry, job_id),
            )

        if status == "RETRY_WAIT":
            record_metric("jobs_retried_total")
            record_metric("events_retried_total")
        else:
            record_metric("jobs_dead_letter_total")
            record_metric("events_failed_total")

        set_gauge("retry_wait_jobs", self.status_counts().get("RETRY_WAIT", 0))
        set_gauge("dead_letter_jobs", self.status_counts().get("DEAD_LETTER", 0))
        return status

    def renew_lease(self, job_id: str, worker_id: str = "in_process", lease_seconds: int = 300) -> bool:
        lease_until = utc_now() + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET lease_until = ?
                WHERE job_id = ? AND status IN ('PROCESSING', 'processing') AND (worker_id = ? OR worker_id IS NULL OR worker_id = 'in_process')
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
            record_metric("events_recovered_total")
            set_gauge("retry_wait_jobs", self.status_counts().get("RETRY_WAIT", 0))
        return count

    def recover_stale_events(self, stale_timeout_seconds: int = 300) -> int:
        return self.recover_expired()

    @staticmethod
    def recover_expired_locked(connection: sqlite3.Connection, now: datetime) -> int:
        cursor = connection.execute(
            """
            UPDATE jobs
            SET status = 'RETRY_WAIT', next_retry_at = ?, last_error = 'worker lease expired',
                worker_id = NULL, lease_until = NULL
            WHERE status IN ('PROCESSING', 'processing') AND (lease_until IS NOT NULL AND lease_until <= ?)
            """,
            (iso(now), iso(now)),
        )
        return cursor.rowcount

    def list_jobs(self, limit: int = 100) -> List[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_event(row) for row in rows]

    def list_events(self, limit: int = 100) -> List[EventRecord]:
        return self.list_jobs(limit)

    def status_counts(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        return {row["status"]: int(row["count"]) for row in rows}

    def list_dead_letter(self, limit: int = 100) -> List[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE status IN ('DEAD_LETTER', 'failed') ORDER BY last_failed_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def requeue_dead_letter(self, job_id: str) -> EventRecord:
        now = utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(f"SELECT {JOB_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                connection.execute("ROLLBACK")
                raise KeyError(f"Unknown job ID: {job_id}")
            if row["status"] not in ("DEAD_LETTER", "failed"):
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
        return self._row_to_event(updated)

    def cleanup_retention(self, retention_days: int) -> int:
        cutoff = utc_now() - timedelta(days=retention_days)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('COMPLETED', 'OBSOLETE', 'DEAD_LETTER', 'completed', 'obsolete', 'failed')
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
    def _row_to_event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(**dict(row))


# Aliases
JobStore = EventStore
JobRecord = EventRecord
Job = EventRecord
