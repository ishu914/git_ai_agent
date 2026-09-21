# Project Context for AI Coding Agents

## 1. Purpose

This repository is a GitLab AI review service for self-hosted GitLab. It receives Merge Request webhook events, persists them durably in a local SQLite queue, re-fetches the authoritative MR state from GitLab, runs deterministic validation checks, optionally calls provider-backed AI review logic, and then updates the AI-managed section of the MR description and/or posts a review note.

The design is intentionally centralized and operationally small: a single FastAPI service, an independent worker process, a SQLite-backed job queue, and provider-neutral AI orchestration.

## 2. Explicit architectural constraints

Do not redesign this around PostgreSQL, a queue broker, or a dashboard stack unless the user explicitly asks for a new architecture.

The active design is:

- FastAPI receiver API for webhook intake
- SQLite durable job store (WAL mode, transactional writes, lease-based processing)
- background worker for asynchronous MR processing
- GitLab client that only exposes fixed read/write operations
- deterministic validation before AI review
- provider-aware AI orchestrator with caching, fallback, and token-budgeting
- MR description updates only in a designated AI-managed section
- observability via structured logs and metrics, not full monitoring infrastructure

This project is currently in a production-hardening, evaluation, and observability phase; it is not a greenfield app rewrite.

## 3. Core runtime flow

1. GitLab sends a Merge Request webhook to the FastAPI app.
2. The webhook layer validates signature/timestamp and ignores irrelevant or AI-generated events.
3. Validated events are persisted into the durable queue (`JobStore`).
4. The background worker claims jobs, recovers expired leases, retries transient failures, and dead-letters exhausted jobs.
5. The worker re-fetches the current MR state from GitLab before processing.
6. Deterministic validation runs first.
7. If the MR is eligible, AI review logic runs with diff-first payload compaction and budget controls.
8. The AI-managed section in the MR description is replaced in place; notes are created or updated as needed.
9. Structured logs and metrics record queue, worker, webhook, and review outcomes.

## 4. Relevant files and responsibilities

### Application bootstrap and configuration

- `app/config.py` — central settings object loaded from environment and `.env`
- `app/main.py` — FastAPI app creation, health/readiness/metrics endpoints, security headers
- `app/project_config.py` — project-scoped YAML config loader
- `config/projects/default.yaml` — default validation configuration

### Webhook and queue

- `app/api/webhook.py` — signature validation, timestamp validation, event relevance checks, dedupe handling, queue submission
- `app/worker/models.py` — durable `Job` model
- `app/worker/store.py` — SQLite-backed queue implementation with claim/retry/dead-letter/retention and backup support
- `app/worker/runner.py` — worker loop, lease renewal, backoff, graceful shutdown

### GitLab and MR processing

- `app/gitlab/client.py` — fixed GitLab API wrapper for project/MR reads and controlled write operations
- `app/services/mr_processor.py` — authoritative MR processing pipeline
- `app/services/description_manager.py` — AI-managed description section replacement logic

### Validation and review logic

- `app/validation/engine.py` — deterministic validation checks for diff size, file count, blocked files, extensions, and sensitive paths
- `app/ai/orchestrator.py` — provider-aware AI orchestration, cooldowns, fallback, token accounting, caching
- `app/ai/client.py` — OpenRouter integration and structured JSON parsing
- `app/ai/groq_client.py` — Groq OpenAI-compatible provider client
- `app/ai/model_discovery.py` — model catalog discovery and cache
- `app/ai/token_budget.py` — diff-first payload compaction, redaction, dedupe, fingerprinting
- `app/ai/evaluation.py` — deterministic evaluation logic used by testing
- `app/ai/prompts.py` — system/user prompt builders

### Observability and safety

- `app/observability.py` — structured logging and metric snapshot/registry
- `app/security` is not a separate package; security logic is spread across webhook validation, GitLab client usage, and prompt/data handling

## 5. Important implementation facts

### Durable queue semantics

- Jobs are persisted in SQLite and can be inspected via the `JobStore` API.
- The queue is designed for a single-server production model, not a distributed multi-host model.
- The worker recovers expired leases on startup and during claims.
- Transient failures enter `RETRY_WAIT`; persistent or exhausted failures enter `DEAD_LETTER`.
- The job schema version is tracked with `SCHEMA_VERSION` and the store rejects unsupported future schema versions.
- Pending jobs for the same project/MR are coalesced when a newer event arrives; the worker still fetches the current MR state before processing.

### Security posture

- GitLab webhook signature validation uses the raw request body and the configured signing token.
- Timestamp tolerance is enforced for signed webhook requests.
- DTOs and prompts treat repository content as untrusted data.
- The AI has no tools and cannot issue arbitrary GitLab API calls through the app.
- The GitLab client intentionally exposes only limited read/write operations.
- Secrets and tokens are never hardcoded in code or committed in `.env`.

### AI review scope

- Deterministic validation runs before AI review.
- Review payloads are compact, diff-first, and redacted for common credential patterns.
- Review fingerprints prevent redundant AI calls for unchanged content.
- Partial reviews are possible when the configured budget excludes some files or content.
- The system records conservative provider metadata without storing raw secret-bearing responses.

## 6. Runtime configuration

Configuration is loaded from environment variables plus `.env` via `app/config.py`.

Required / commonly used keys include:

- `GITLAB_URL`
- `GITLAB_TOKEN`
- `GITLAB_AI_USERNAME`
- `GITLAB_WEBHOOK_SIGNING_TOKEN` or legacy `GITLAB_WEBHOOK_SECRET`
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_ENABLED`
- `GROQ_API_KEY`, `GROQ_ENABLED`
- `WORKER_DATABASE_PATH`
- `WORKER_CONCURRENCY`, `WORKER_MAX_ATTEMPTS`, `WORKER_LEASE_SECONDS`
- `AI_*` token and review-budget settings

Keep the real `.env` local and uncommitted; only `.env.example` should be a checked-in template.

## 7. Commands to know

### Run the app

```bash
python -m app.main
```

or

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### Run the worker

```bash
python -m app.worker.runner
```

### Worker maintenance commands

```bash
python -m app.worker.runner --status
python -m app.worker.runner --list-dead-letter
python -m app.worker.runner --retry-job JOB_ID
python -m app.worker.runner --cleanup-retention
python -m app.worker.runner --backup /var/backups/git-ai-agent/jobs.sqlite3
```

### Tests

```bash
python -m pytest -q
```

or targeted suites such as:

```bash
python -m pytest tests/evaluation -q
```

## 8. Validation status

The repository is currently validated in the working environment with pytest and static checks. It should be treated as passing unless a fresh run shows otherwise.

The last verified state in this workspace was:

- pytest: passing for the current repo state
- static analysis: no immediate file errors reported

Before modifying production behavior, run the relevant test subset and check the specific service area you changed.

## 9. Interaction guidance for future agents

- Preserve the existing architecture and queue semantics unless the user explicitly asks for a different design.
- Keep changes minimal and related to the asked task.
- Prefer reading and extending existing modules over introducing new abstractions or infrastructure.
- Do not broaden scope into dashboards, database migration, or unrelated service rewrites.
- Preserve security boundaries: do not add arbitrary GitLab access, shell execution, or side-effectful actions outside the defined API contract.
- Keep logs structured and avoid leaking secrets or token values.
- Maintain compatibility with the durable SQLite job store and its explicit state transitions.

## 10. Current project status

This repository is already beyond the initial bootstrap phase. It includes durable queue behavior, worker processing, security validation, AI review orchestration, deterministic evaluation, operational observability, and Phase 6 deployment-hardening documentation.

The active deployment model is a single-server Linux setup with two independently managed services: a webhook receiver and a worker. Both rely on the same durable SQLite store; neither is a redesign of the application architecture. Deployment files are documented under [deploy/systemd](deploy/systemd) and the production deployment guide is under [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

The immediate priority is to preserve this working implementation and document the exact service layout, filesystem permissions, backup pattern, restoration workflow, and Linux verification steps needed before claiming production readiness.

## 11. Phase 6 deployment architecture

The production deployment model for this repo is intentionally conservative:

- one Linux server
- GitLab and the GitLab AI reviewer share a trusted internal network
- receiver service handles HTTP/webhook intake, validation, and durable job creation
- worker service handles queue polling, job claiming, MR processing, AI review, and GitLab updates
- SQLite remains the durable queue implementation; no migration to PostgreSQL is introduced
- configuration remains environment-driven and stored outside the repo in a service-protected file such as `/etc/git-ai-reviewer/agent.env`
- backup and retention jobs are scheduled externally through systemd timers or a simple cron equivalent
- logs remain structured and secret-safe; filesystem paths are service-owned and restricted

This phase documents deployment readiness, not a new architecture. Actual Linux runtime behavior must still be verified on the target host.

## 12. Deployment artifacts in this repo

- [deploy/systemd/git-ai-web.service](deploy/systemd/git-ai-web.service) — receiver service template
- [deploy/systemd/git-ai-worker.service](deploy/systemd/git-ai-worker.service) — worker service template
- [deploy/systemd/git-ai-backup.service](deploy/systemd/git-ai-backup.service) — manual backup entry point
- [deploy/systemd/git-ai-backup.timer](deploy/systemd/git-ai-backup.timer) — daily backup schedule template
- [deploy/systemd/git-ai-retention.service](deploy/systemd/git-ai-retention.service) — retention cleanup entry point
- [deploy/systemd/git-ai-retention.timer](deploy/systemd/git-ai-retention.timer) — weekly retention schedule template
- [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) — Linux deployment procedures, permissions, startup order, rollback, and smoke test guidance

These artifacts are deployment-ready templates and documentation, not a claim that the production host has already been configured.

## 13. Phase 7 progress status

### COMPLETED

- durable queue retry-wait and lease recovery checks
- GitLab API failure coverage for retryable vs permanent errors
- AI provider failure coverage for timeout, quota, empty output, malformed JSON, incomplete responses, and fallback behavior
- security-abuse and untrusted-input validation
- SQLite initialization, backup, restore, and retention safety checks covering the current offline contract
- deterministic offline verification of the current Phase 7 subset

### IMPLEMENTED BUT NOT VERIFIED

- live Linux filesystem permission and systemd timer validation for the backup/retention deployment model
- load/concurrency scenarios beyond the deterministic SQLite queue contracts already executed
- observability-focused failure verification beyond the current subset

### PLANNED

- final recovery and rollback validation on a Linux host
- concurrency and load pressure testing
- observability-focused failure verification
- final recovery/integration review

### NOT IMPLEMENTED

- Phase 8 live AI evaluation
- new providers or architecture changes
- PostgreSQL migration
- any change that broadens beyond the existing webhook/worker design

The current Phase 7 slice is validated only for the offline failure/recovery and SQLite backup/restore checks actually executed in this workspace. It is not marked as fully complete overall, and live provider testing remains pending.
