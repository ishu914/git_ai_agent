import asyncio
import argparse
import contextlib
import json
import logging
import signal
import socket
import time
from types import SimpleNamespace
from typing import Optional

from app.config import get_settings
from app.observability import log_event, record_metric
from app.services.mr_processor import process_gitlab_event
from app.worker.models import Job
from app.worker.store import JobStore

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, store: Optional[JobStore] = None, worker_id: Optional[str] = None) -> None:
        settings = get_settings()
        legacy_defaults = {
            "worker_database_path": "data/jobs.sqlite3",
            "worker_concurrency": 2,
            "worker_lease_seconds": 900,
            "worker_poll_interval_seconds": 2.0,
            "worker_max_attempts": 3,
            "worker_backoff_base_seconds": 5,
            "worker_backoff_max_seconds": 300,
            "worker_job_retention_days": 90,
        }
        settings_values = settings.model_dump()
        for name, default in legacy_defaults.items():
            settings_values.setdefault(name, default)
        self.settings = SimpleNamespace(**settings_values)
        self.store = store or JobStore(self.settings.worker_database_path)
        self.worker_id = worker_id or f"{socket.gethostname()}-{id(self)}"
        self._stop = False
        self._semaphore = asyncio.Semaphore(min(self.settings.worker_concurrency, self.settings.ai_max_concurrent_reviews))
        recovered = self.store.recover_expired()
        if recovered:
            record_metric("jobs_recovered_total")
            log_event("JOB_RECOVERED", worker_id=self.worker_id, count=recovered)
            logger.info("JOB_RECOVERED count=%s worker_id=%s", recovered, self.worker_id)

    async def process_one(self) -> Optional[Job]:
        job = self.store.claim(self.worker_id, self.settings.worker_lease_seconds)
        if not job:
            return None
        record_metric("jobs_claimed_total")
        log_event("JOB_CLAIMED", job_id=job.job_id, project_id=job.project_id, mr_iid=job.mr_iid, worker_id=self.worker_id, attempt=job.attempt_count)
        return await self._process_claimed(job)

    async def run_once(self) -> Optional[Job]:
        return await self.process_one()

    async def run_forever(self) -> None:
        active: set[asyncio.Task] = set()
        logger.info("WORKER_STARTED worker_id=%s", self.worker_id)
        while not self._stop:
            active = {task for task in active if not task.done()}
            while len(active) < self.settings.worker_concurrency:
                job = self.store.claim(self.worker_id, self.settings.worker_lease_seconds)
                if not job:
                    break
                active.add(asyncio.create_task(self._process_claimed(job)))
            if not active:
                await asyncio.sleep(self.settings.worker_poll_interval_seconds)
        logger.info("WORKER_STOPPING worker_id=%s active=%s", self.worker_id, len(active))
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        logger.info("WORKER_STOPPED worker_id=%s", self.worker_id)

    async def _process_claimed(self, job: Job) -> Optional[Job]:
        async with self._semaphore:
            started = time.monotonic()
            heartbeat = asyncio.create_task(self._renew_lease(job))
            try:
                log_event("JOB_STARTED", job_id=job.job_id, project_id=job.project_id, mr_iid=job.mr_iid, worker_id=self.worker_id, attempt=job.attempt_count)
                payload = json.loads(job.payload)
                await process_gitlab_event(payload)
                self.store.complete(job.job_id)
                record_metric("jobs_completed_total")
                log_event("JOB_COMPLETED", job_id=job.job_id, project_id=job.project_id, mr_iid=job.mr_iid, worker_id=self.worker_id, duration_ms=int((time.monotonic() - started) * 1000))
                logger.info(
                    "Worker completed job: job_id=%s project_id=%s mr_iid=%s attempt=%s worker_id=%s duration_ms=%s",
                    job.job_id, job.project_id, job.mr_iid, job.attempt_count, self.worker_id,
                    int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                permanent = self._is_permanent_error(exc)
                self.store.fail(
                    job.job_id,
                    str(exc),
                    self.settings.worker_max_attempts,
                    self.settings.worker_backoff_base_seconds,
                    self.settings.worker_backoff_max_seconds,
                    permanent,
                )
                record_metric("jobs_failed_total")
                log_event("JOB_FAILED", level="ERROR", job_id=job.job_id, project_id=job.project_id, mr_iid=job.mr_iid, worker_id=self.worker_id, attempt=job.attempt_count, error_category="internal", permanent=permanent)
                logger.exception(
                    "Worker failed job: job_id=%s project_id=%s mr_iid=%s attempt=%s worker_id=%s permanent=%s",
                    job.job_id, job.project_id, job.mr_iid, job.attempt_count, self.worker_id, permanent,
                )
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
        return self.store.get(job.job_id)

    async def _renew_lease(self, job: Job) -> None:
        interval = max(1, self.settings.worker_lease_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            if not self.store.renew_lease(job.job_id, self.worker_id, self.settings.worker_lease_seconds):
                return

    def stop(self) -> None:
        self._stop = True
        record_metric("worker_shutdown_total")
        log_event("WORKER_STOPPING", worker_id=self.worker_id)

    @staticmethod
    def _is_permanent_error(error: Exception) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in ("401", "403", "invalid project", "invalid mr", "malformed request", "unsupported model"))


def _print_job(job: Job) -> None:
    print(
        f"{job.job_id} project={job.project_id} mr=!{job.mr_iid} event={job.event_type} "
        f"status={job.status} attempts={job.attempt_count} created={job.created_at} "
        f"last_error={job.last_error or '-'}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="GitLab AI Agent durable job worker")
    parser.add_argument("--status", action="store_true", help="show queue status counts")
    parser.add_argument("--list-dead-letter", action="store_true", help="show dead-letter jobs")
    parser.add_argument("--retry-job", metavar="JOB_ID", help="requeue one dead-letter job")
    parser.add_argument("--cleanup-retention", action="store_true", help="remove old historical jobs")
    parser.add_argument("--backup", metavar="PATH", help="create a consistent SQLite backup")
    args = parser.parse_args()
    settings = get_settings()
    settings.validate_required_runtime()
    store = JobStore(settings.worker_database_path)
    if args.status:
        for status, count in sorted(store.status_counts().items()):
            print(f"{status}: {count}")
        return
    if args.list_dead_letter:
        for job in store.list_dead_letter():
            _print_job(job)
        return
    if args.retry_job:
        try:
            job = store.requeue_dead_letter(args.retry_job)
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        print(f"Requeued job {job.job_id}")
        return
    if args.cleanup_retention:
        print(f"Removed {store.cleanup_retention(settings.worker_job_retention_days)} historical jobs")
        return
    if args.backup:
        store.backup(args.backup)
        print(f"Backup created at {args.backup}")
        return

    logging.basicConfig(level=logging.INFO)
    worker = JobWorker()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(stop_signal, worker.stop)
    try:
        loop.run_until_complete(worker.run_forever())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
