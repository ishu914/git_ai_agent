from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class Job:
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
    started_at: Optional[str]
    completed_at: Optional[str]
    next_retry_at: Optional[str]
    last_error: Optional[str]
    retry_classification: Optional[str]
    last_failed_at: Optional[str]
    review_fingerprint: Optional[str]
    worker_id: Optional[str]
    lease_until: Optional[str]
