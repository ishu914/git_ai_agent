import json

import pytest

from app.observability import MetricsRegistry, log_event, metric_snapshot, record_metric, sanitize_for_log


def test_structured_log_sanitizes_sensitive_fields():
    payload = sanitize_for_log({"token": "secret", "user": "alice", "nested": {"Authorization": "bearer"}})

    assert payload["token"] == "[REDACTED]"
    assert payload["nested"]["Authorization"] == "[REDACTED]"


def test_metrics_registry_tracks_counter_and_gauge():
    metrics = MetricsRegistry()
    metrics.increment("webhook_received_total")
    metrics.set_gauge("pending_jobs", 3)

    snapshot = metrics.snapshot()
    assert snapshot["counters"]["webhook_received_total"] == 1
    assert snapshot["gauges"]["pending_jobs"] == 3


def test_log_event_has_structure_and_no_sensitive_keys(caplog):
    with caplog.at_level("INFO"):
        log_event("JOB_CREATED", job_id="job-1", project_id="4", mr_iid=17, token="should-not-be-logged")

    message = caplog.records[0].message
    assert "JOB_CREATED" in message
    assert "should-not-be-logged" not in message
    assert "token" in message


def test_metric_snapshot_uses_bounded_labels_only():
    record_metric("provider_requests_total")
    record_metric("provider_requests_total")

    snapshot = metric_snapshot()
    assert snapshot["counters"]["provider_requests_total"] == 2
