import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Set

from app.config import Settings, get_settings
from app.events.errors import classify_error
from app.events.store import EventRecord, EventStore
from app.observability import log_event, record_metric, set_gauge
from app.services import mr_processor

logger = logging.getLogger(__name__)

_global_event_manager: Optional["InProcessEventManager"] = None


class InProcessEventManager:
    """Manages in-process event execution, concurrency, crash recovery, and graceful shutdown."""

    def __init__(self, store: Optional[EventStore] = None, settings: Optional[Settings] = None) -> None:
        self.settings = settings or get_settings()
        self.store = store or EventStore(self.settings.database_path)
        self.semaphore = asyncio.Semaphore(self.settings.max_concurrent_mr_jobs)
        self.active_tasks: Set[asyncio.Task] = set()
        self.processing_keys: Set[str] = set()
        self._shutting_down = False
        self._notify_event = asyncio.Event()
        self._dispatcher_task: Optional[asyncio.Task] = None
        # Only MR jobs use this executor. Its bound matches the dispatcher
        # limit, so queued webhook requests cannot exhaust processing threads.
        self._processor_executor = ThreadPoolExecutor(
            max_workers=self.settings.max_concurrent_mr_jobs,
            thread_name_prefix="mr-processor",
        )

    async def start(self) -> None:
        """Start the in-process event manager: recover stale events and start dispatcher loop."""
        self._shutting_down = False
        # Crash recovery on startup
        recovered_count = self.store.recover_stale_events(self.settings.stale_processing_timeout_seconds)
        if recovered_count:
            logger.info("Startup crash recovery: reset %d stale processing events", recovered_count)
            log_event("STARTUP_CRASH_RECOVERY", level="INFO", count=recovered_count)

        self._dispatcher_task = asyncio.create_task(self._dispatcher_loop())
        logger.info("InProcessEventManager started with max_concurrent_mr_jobs=%d", self.settings.max_concurrent_mr_jobs)

    def notify_new_event(self) -> None:
        """Wake up dispatcher immediately when a new event is persisted."""
        self._notify_event.set()

    async def _dispatcher_loop(self) -> None:
        """Background loop claiming events from SQLite store and scheduling them in-process."""
        while not self._shutting_down:
            try:
                # Clean finished tasks
                self.active_tasks = {t for t in self.active_tasks if not t.done()}
                set_gauge("active_mr_jobs", len(self.active_tasks))

                while len(self.active_tasks) < self.settings.max_concurrent_mr_jobs and not self._shutting_down:
                    event = self.store.claim_next(lease_seconds=self.settings.stale_processing_timeout_seconds)
                    if not event:
                        break

                    if event.dedupe_key in self.processing_keys:
                        # Prevent duplicate concurrent execution of identical dedupe key
                        continue

                    task = asyncio.create_task(self._execute_event(event))
                    self.active_tasks.add(task)

                self._notify_event.clear()
                # Wait for notification or periodic poll (e.g. 1 second)
                try:
                    await asyncio.wait_for(self._notify_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.exception("Unexpected error in event dispatcher loop: %s", exc)
                await asyncio.sleep(1.0)

    async def _execute_event(self, event: EventRecord) -> None:
        """Execute a single claimed event within semaphore limits."""
        self.processing_keys.add(event.dedupe_key)
        start_time = time.monotonic()
        try:
            async with self.semaphore:
                log_event(
                    "PROCESSING_STARTED",
                    level="INFO",
                    event_type="merge_request",
                    result="started",
                    project_id=event.project_id,
                    mr_iid=event.mr_iid,
                    webhook_id=event.dedupe_key,
                )
                payload = json.loads(event.payload)
                payload["_dedupe_key"] = event.dedupe_key
                payload["webhook_id"] = event.dedupe_key

                # The current GitLab and provider clients are synchronous. Run
                # the complete legacy processing stack outside FastAPI's event
                # loop until their public interfaces can be migrated to async.
                # This dispatcher remains the sole invocation path.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._processor_executor, _run_processor, payload)

                # Mark completed in durable event store
                self.store.complete(event.event_id)
                duration = time.monotonic() - start_time
                record_metric("events_completed_total")
                log_event(
                    "PROCESSING_COMPLETED",
                    level="INFO",
                    event_type="merge_request",
                    result="completed",
                    project_id=event.project_id,
                    mr_iid=event.mr_iid,
                    webhook_id=event.dedupe_key,
                    duration_seconds=round(duration, 3),
                )
                logger.info(
                    "Completed event processing: event_id=%s dedupe_key=%s duration_s=%.3f",
                    event.event_id,
                    event.dedupe_key,
                    duration,
                )

        except Exception as exc:
            duration = time.monotonic() - start_time
            status = self.store.fail(
                event.event_id,
                exc,
                max_attempts=self.settings.event_max_attempts,
                backoff_base=self.settings.event_retry_backoff_base_seconds,
                backoff_max=self.settings.event_retry_backoff_max_seconds,
            )
            category, is_perm = classify_error(exc)
            log_event(
                "PROCESSING_FAILED",
                level="ERROR",
                event_type="merge_request",
                result="failed",
                project_id=event.project_id,
                mr_iid=event.mr_iid,
                webhook_id=event.dedupe_key,
                error_category=category,
                permanent=is_perm,
                final_status=status,
                duration_seconds=round(duration, 3),
            )
            logger.warning(
                "Event processing failed: event_id=%s dedupe_key=%s status=%s category=%s error=%s",
                event.event_id,
                event.dedupe_key,
                status,
                category,
                str(exc)[:200],
            )
        finally:
            self.processing_keys.discard(event.dedupe_key)
            set_gauge("active_mr_jobs", max(0, len(self.active_tasks) - 1))

    async def shutdown(self) -> None:
        """Gracefully shut down in-process event execution.

        1. Stop accepting new background MR processing work.
        2. Allow already accepted/persisted events to remain durable.
        3. Stop starting new processing tasks.
        4. Wait for active tasks to finish up to shutdown_timeout_seconds.
        """
        self._shutting_down = True
        logger.info("Graceful shutdown initiated (timeout=%ds)...", self.settings.shutdown_timeout_seconds)

        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()

        active = [t for t in self.active_tasks if not t.done()]
        if active:
            logger.info("Waiting for %d active MR processing tasks to finish...", len(active))
            try:
                await asyncio.wait_for(
                    asyncio.gather(*active, return_exceptions=True),
                    timeout=self.settings.shutdown_timeout_seconds,
                )
                logger.info("All active tasks completed gracefully within shutdown timeout.")
            except asyncio.TimeoutError:
                logger.warning(
                    "Shutdown timeout reached (%ds); cancelling remaining active tasks. Events remain durable for crash recovery.",
                    self.settings.shutdown_timeout_seconds,
                )
                for task in active:
                    if not task.done():
                        task.cancel()
        self._processor_executor.shutdown(wait=False, cancel_futures=True)


def get_event_manager() -> InProcessEventManager:
    global _global_event_manager
    if _global_event_manager is None:
        _global_event_manager = InProcessEventManager()
    return _global_event_manager


def set_event_manager(manager: Optional[InProcessEventManager]) -> None:
    global _global_event_manager
    _global_event_manager = manager


def _run_processor(payload: Dict[str, Any]) -> Any:
    """Run the async processor in a dedicated worker thread."""
    return asyncio.run(mr_processor.process_gitlab_event(payload))
