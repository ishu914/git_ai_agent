# GitLab AI Agent — Phase 9 Production Reliability & Security Deployment Guide

## 1. Architecture Overview

The GitLab AI Agent operates on a **Single-Process / Single-Service Architecture**:

```
GitLab Webhook (HTTPS)
       ↓
Reverse Proxy (Nginx / Caddy TLS Termination)
       ↓ HTTP
FastAPI / Uvicorn (127.0.0.1:8000)
       ↓
SQLite Durable Event Store (data/events.sqlite3) [In-Process]
       ↓
In-Process Event Manager & Concurrency Control
       ↓
GitLab API + Validation Engine + AI Provider Fallback
       ↓
MR Description Update & Review Note
```

- **One Application**: FastAPI app.
- **One Process**: Uvicorn process listening on `127.0.0.1:8000`.
- **One Systemd Service**: `git-ai-agent.service`.
- **In-Process Durable Event Persistence**: Accepted webhooks are persisted to SQLite (`data/events.sqlite3`) before HTTP 200 is returned.

---

## 2. Durable Event Store & Crash Recovery

### Event Lifecycle States
Events move through the following lifecycle:
- `pending`: Received and persisted in SQLite before acknowledging webhook.
- `processing`: Claimed by in-process manager for execution.
- `completed`: Successfully processed.
- `retry`: Failed due to transient error, scheduled for exponential backoff retry.
- `failed`: Dead-lettered after reaching maximum attempts or encountering permanent error.
- `obsolete`: Replaced by a newer event for the same Merge Request.

### Crash Recovery Protocol
On application startup (`lifespan` startup handler):
1. `EventStore.recover_stale_events(stale_timeout_seconds=300)` inspects events left in `processing` state by a previous crash.
2. Stale events are reset to `retry` state and scheduled for execution.
3. Unfinished tasks are never lost across process restarts.

---

## 3. Reverse Proxy & TLS Termination

Raw Uvicorn must **never** be exposed directly to untrusted external networks. HTTPS must terminate at a reverse proxy.

### Nginx Reverse Proxy Configuration
Add the following block to `/etc/nginx/sites-available/gitlab-ai-agent`:

```nginx
server {
    listen 443 ssl http2;
    server_name git-ai-agent.internal.domain;

    ssl_certificate /etc/ssl/certs/git-ai-agent.crt;
    ssl_certificate_key /etc/ssl/private/git-ai-agent.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    client_max_body_size 2M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

- Webhook URL configured in GitLab: `https://git-ai-agent.internal.domain/webhook/gitlab`
- Uvicorn binding: `127.0.0.1:8000` (loopback interface only).

---

## 4. Graceful Shutdown & Systemd Coordination

### Timeout Coordination
- **Application Shutdown Timeout**: `SHUTDOWN_TIMEOUT_SECONDS=15`
- **Systemd Stop Timeout**: `TimeoutStopSec=30`

When systemd sends `SIGTERM`:
1. FastAPI lifespan shutdown triggers `InProcessEventManager.shutdown()`.
2. Stop scheduling new tasks.
3. Wait up to 15 seconds for active MR processing tasks to finish cleanly.
4. Systemd allows up to 30 seconds before sending `SIGKILL`. If tasks do not finish within 15s, remaining active task state remains saved in SQLite as `processing` and will be recovered automatically on restart.

---

## 5. Webhook Security & Protection

1. **Request Body Size Limit**: `WEBHOOK_MAX_BODY_BYTES=1048576` (1MB). Payloads exceeding 1MB are rejected with HTTP 413 Payload Too Large.
2. **Webhook Authentication**:
   - Standard Webhook signing tokens (`GITLAB_WEBHOOK_SIGNING_TOKEN=whsec_...` using HMAC-SHA256).
   - Legacy secret token (`GITLAB_WEBHOOK_SECRET=...` using constant-time `hmac.compare_digest`).
3. **Concurrency Control**: `WEBHOOK_MAX_CONCURRENT_REQUESTS=10` bounds concurrent webhook request handling. `MAX_CONCURRENT_MR_JOBS=2` bounds dispatcher jobs. Processing runs outside the FastAPI event loop, so `/health`, `/ready`, and `/metrics` remain responsive while GitLab or AI operations are slow.
4. **Queue Backpressure**: `EVENT_MAX_QUEUE_DEPTH=1000` limits pending and retrying durable events. Once full, new unique deliveries receive HTTP 429; duplicate deliveries still receive their normal idempotent response.

---

## 6. AI Data Governance & Project Policy

### Configuration
Controlled globally via environment:
```env
AI_EXTERNAL_PROVIDERS_ALLOWED=true
```

And overridden on a per-project basis in `config/projects/<project-name>.yaml`:
```yaml
ai:
  external_providers_allowed: false
```

### Behavior
- If `external_providers_allowed` is `false`, source code and diffs are **never** sent to external providers (OpenRouter / Groq).
- If no internal provider (such as local AECO_AI) is configured, the agent fails safely with status `"unavailable"` and error category `"configuration"`.

---

## 7. Secret Rotation Procedures

### GitLab service account and PAT
The API endpoints used by this service read projects, merge requests, diffs, commits, approvals and files; they update an MR description and create or update MR notes. A classic PAT requires the `api` scope for these write endpoints; token scope is separate from project membership.

On the current GitLab roles matrix, **Developer is the minimum built-in project role that can update merge-request details**, while comments can be added by lower roles. Developer is therefore required by the current description-update design. This role can also receive merge capability depending on protected-branch configuration, and GitLab's built-in roles do not provide a role that both updates MR details and categorically lacks all approval/merge-related ability. Do not represent this account as least-privilege if that capability is unacceptable.

Restrict the service account to only reviewed projects, protect every target branch with **Allowed to merge: Maintainers only** and **Allowed to push and merge: No one**, and use a dedicated account with no Maintainer/Owner membership. The application contains no approve, merge, close, assign, label, commit, or branch mutation endpoint. GitLab does not expose a stable REST startup check proving all effective merge or approval permissions, because they depend on branch protection and project policy; validate those controls with a non-production MR after configuring the target GitLab instance.

### Rotating GitLab PAT
1. Generate a new Personal Access Token with the `api` scope for the restricted service account.
2. Update `/etc/git-ai-reviewer/agent.env`:
   `GITLAB_TOKEN=glpat-newtoken...`
3. Restart service: `sudo systemctl restart git-ai-agent`

### Rotating Webhook Signing Token
1. Generate new signing token (`whsec_...`) in GitLab.
2. Update `/etc/git-ai-reviewer/agent.env`:
   `GITLAB_WEBHOOK_SIGNING_TOKEN=whsec_newtoken...`
3. Restart service: `sudo systemctl restart git-ai-agent`

### Rotating OpenRouter / Groq Keys
1. Generate key in provider console.
2. Update `OPENROUTER_API_KEY` or `GROQ_API_KEY` in `/etc/git-ai-reviewer/agent.env`.
3. Restart service: `sudo systemctl restart git-ai-agent`

---

## 8. Backup & Database Retention

All maintenance operations use `tools/manage_events.py` — a standalone CLI that
operates directly against the `EventStore`. **No worker process, no systemd timer,
no additional service is required.**

### Retention Policy
- `EVENT_RETENTION_DAYS=90`: Purges `completed`, `failed`, and `obsolete` events older than 90 days.
- Active (`pending`, `processing`, `retry`) events are **never** deleted.

Run manually from the application directory (cron or systemd oneshot if desired):
```bash
/opt/git-ai-reviewer/venv/bin/python -m tools.manage_events --cleanup-retention
```

### SQLite Backup Procedure
SQLite WAL mode allows live hot backups without locking reads or writes:
```bash
/opt/git-ai-reviewer/venv/bin/python -m tools.manage_events \
  --backup /var/backups/git-ai-agent/events-$(date +%F).sqlite3
```

### Other Maintenance Commands
```bash
# Show event queue status counts
/opt/git-ai-reviewer/venv/bin/python -m tools.manage_events --status

# List dead-letter events
/opt/git-ai-reviewer/venv/bin/python -m tools.manage_events --list-dead-letter

# Manually requeue a dead-letter event by ID
/opt/git-ai-reviewer/venv/bin/python -m tools.manage_events --retry-job <JOB_ID>
```

---

## 9. Observability & Health Check Endpoints

- **`/health`**: Fast lightweight process health check (200 OK). No external API calls.
- **`/ready`**: Verifies SQLite database connection, configuration validity, and AI provider availability.
- **`/metrics`**: Exposes Prometheus-style metric snapshots (`webhook_received_total`, `events_persisted_total`, `events_completed_total`, `events_failed_total`, `active_mr_jobs`).

---

## 10. Limitations & Atomic Operations

GitLab REST API does not support multi-resource atomic transactions across MR description and note endpoints.
- Sequencing: MR Description is updated first, followed by MR Review Note creation/update.
- Idempotency: Description updates replace only marked AI sections. Review notes check for existing `<!-- AI_REVIEW_NOTE -->` markers before editing, preventing duplicate notes during retries.
