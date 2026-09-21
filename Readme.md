# GitLab AI Agent

A central, reusable GitLab AI agent designed for self-hosted GitLab installations. This repository contains the initial Phase 1 foundation for the service: application bootstrap, settings loading, and a health check endpoint.

## Architecture

The end-state design is a central webhook-driven service that:

- receives GitLab Merge Request webhook events
- validates the signature and payload
- fetches MR metadata and diff from GitLab
- runs deterministic validation checks
- triggers AI review and summary generation through a provider-neutral AI orchestrator
- discovers eligible OpenRouter free models dynamically and falls back across models
- uses Groq as an optional secondary OpenAI-compatible provider
- updates only the AI-managed section in the MR description
- checks approval readiness without automating approval or merge

## Requirements

- Python 3.11+
- FastAPI
- Uvicorn
- Python dotenv
- Pydantic settings

## Python installation

From the project root:

```bash
python -m venv .venv
. .venv/bin/activate  # Linux/macOS
# or .venv\Scripts\activate  # Windows PowerShell
pip install --upgrade pip
pip install -r requirements.txt
```

## Environment variables

Create a local `.env` file based on `.env.example`:

```bash
cp .env.example .env
```

The expected configuration values are:

```bash
GITLAB_URL=http://192.168.2.86
GITLAB_TOKEN=
GITLAB_AI_USERNAME=gi_ai_code_reviewer
GITLAB_WEBHOOK_SECRET=

OPENROUTER_API_KEY=
OPENROUTER_MODEL=
OPENROUTER_ENABLED=true
OPENROUTER_MODEL_STRATEGY=dynamic-free
OPENROUTER_FREE_MODEL_MAX_ATTEMPTS=5
OPENROUTER_MODEL_DISCOVERY_CACHE_TTL_SECONDS=3600

GROQ_API_KEY=
GROQ_ENABLED=true
GROQ_MODEL_STRATEGY=dynamic
GROQ_MODEL_MAX_ATTEMPTS=3
GROQ_MODEL_DISCOVERY_CACHE_TTL_SECONDS=3600

AI_TOTAL_MAX_ATTEMPTS=8
AI_MODEL_COOLDOWN_SECONDS=300

AI_AGENT_HOST=0.0.0.0
AI_AGENT_PORT=8000
```

### Notes

- Never commit the `.env` file.
- Keep API keys and secrets in environment variables only.
- The AI model must remain configurable through `OPENROUTER_MODEL`.
- `GITLAB_TOKEN` must belong to the centralized GitLab service account `gi_ai_code_reviewer`, whose display name should be `GI AI Code Reviewer`. This makes MR descriptions and notes appear as `GI AI Code Reviewer (@gi_ai_code_reviewer)` rather than an Administrator user.
- Create the service account once at the GitLab instance/group level with access to the projects it reviews; do not create project-specific application tokens.

## AI provider configuration

The AI orchestrator uses OpenRouter's model catalog to discover current free models. Models are filtered by free pricing or a `:free` variant and cached for the configured TTL. A model that returns a rate limit, quota, timeout, provider error, malformed response, empty response, or incomplete response is cooled down and the next eligible model is tried.

The optional Groq provider uses its model catalog as a secondary fallback. Missing OpenRouter or Groq credentials do not prevent application startup. If every configured candidate fails, deterministic validation still completes and the MR receives an explicit AI-unavailable status.

The orchestrator never approves or merges an MR. Human approval remains separate.

## AI diagnostics

Run the safe inventory command from the project root:

```bash
python -m app.ai.diagnostics
```

It reports whether each provider is configured, discovered model IDs, context lengths, JSON capability metadata, and fallback order. It never prints API keys or request headers.

Useful settings:

- `OPENROUTER_MODEL_STRATEGY=dynamic-free`
- `OPENROUTER_FREE_MODEL_MAX_ATTEMPTS=5`
- `OPENROUTER_MODEL_DISCOVERY_CACHE_TTL_SECONDS=3600`
- `GROQ_MODEL_MAX_ATTEMPTS=3`
- `AI_TOTAL_MAX_ATTEMPTS=8`
- `AI_MODEL_COOLDOWN_SECONDS=300`

## Token-efficient MR analysis

The processor runs deterministic validation first, then sends one compact, diff-first analysis request for both the summary and code review. It excludes generated, binary, dependency, vendor, lock, and unchanged low-value content before the request. The result is reused for the MR description and review note.

Budgets are shared per MR and can be configured with:

- `AI_MAX_INPUT_TOKENS_PER_REQUEST`
- `AI_MAX_OUTPUT_TOKENS_PER_REQUEST`
- `AI_MAX_TOTAL_TOKENS_PER_MR`
- `AI_MAX_DIFF_CHARS`
- `AI_MAX_FILE_CHARS`
- `AI_MAX_CONTEXT_FILES`
- `AI_REVIEW_CACHE_TTL_SECONDS`

When configured limits exclude changed content, the generated result reports `Review Scope: partial` and lists excluded files. A deterministic fingerprint prevents another AI call for unchanged project/MR/review content. Provider usage metadata is recorded when available, with conservative estimates otherwise.

## AI review evaluation

Synthetic deterministic evaluation cases live under `tests/evaluation/fixtures/` and cover security vulnerabilities, bugs, performance, safe code, and prompt injection. `tests/evaluation/test_review_quality.py` evaluates semantic invariants such as required concepts, prohibited false positives, safe file references, severity validity, finding count, and partial review scope. It uses mocked structured results and never calls OpenRouter or Groq.

Run the deterministic evaluation tests with:

```bash
python -m pytest tests/evaluation -q
```

Live provider evaluation is intentionally separate and is not required by CI. Any future live run must use synthetic fixtures, explicit credentials outside the repository, and record provider/model, prompt policy version, configuration, detection results, false positives, malformed outputs, incomplete responses, and partial-scope counts without storing complete provider responses or secrets.

The review fingerprint includes the relevant compact diff, project/MR identity, source commit, and `REVIEW_POLICY_VERSION`. Prompt or schema policy changes can therefore trigger fresh analysis without invalidating unrelated historical work. Webhook jobs retain only the minimal event envelope required to refetch authoritative MR state; MR descriptions and source diffs are not stored in the queue payload.

## GitLab token creation

Use a dedicated bot or service account for GitLab API access. Create a personal access token (PAT) or project/service account token with the smallest permissions required for:

- reading project metadata
- reading merge requests and diffs
- writing MR notes and description updates
- reading approvals where available

For the initial Phase 1, the application does not yet call GitLab APIs or process webhooks. The token is prepared for later phases.

## GitLab webhook configuration

This is the next phase after the current bootstrap service. The webhook endpoint will be implemented as:

```text
POST /webhook/gitlab
```

The service will validate the GitLab webhook signature using the configured secret. The initial implementation will support MR open/update and commit-driven review events.

## Running locally

Start the service with:

```bash
python -m app.main
```

Or through uvicorn directly:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Durable worker queue

Webhook requests persist validated events into the agent's own SQLite database at `WORKER_DATABASE_PATH` and return without running GitLab or AI work. SQLite uses WAL mode and transactional atomic claims. The worker runs separately:

```bash
python -m app.worker.runner
```

The receiver and worker may be restarted independently. Configure `WORKER_CONCURRENCY`, `WORKER_LEASE_SECONDS`, `WORKER_MAX_ATTEMPTS`, and the backoff settings in the environment. Expired leases are recovered, transient failures enter `RETRY_WAIT`, and permanently failed or exhausted jobs enter `DEAD_LETTER`.

Pending jobs for the same project/MR are coalesced when a newer event arrives. The worker still fetches the authoritative current MR state before processing. Inspect queued jobs from Python with `JobStore(WORKER_DATABASE_PATH).list_jobs()`; operational recovery should use the store's explicit state transitions rather than editing GitLab's database.

SQLite is selected for this single-server phase because it is durable, transactional, dependency-free, and easy to operate. It is not a substitute for PostgreSQL under high multi-host write contention; migrate the store behind the same interface before scaling across hosts.

### Queue operations

```bash
python -m app.worker.runner --status
python -m app.worker.runner --list-dead-letter
python -m app.worker.runner --retry-job JOB_ID
python -m app.worker.runner --cleanup-retention
python -m app.worker.runner --backup /var/backups/git-ai-agent/jobs.sqlite3
```

Retention removes only old `COMPLETED`, `OBSOLETE`, and `DEAD_LETTER` jobs. Active, pending, processing, and retryable jobs are retained. Dead-letter requeue is limited to jobs currently in `DEAD_LETTER` state and does not create a new webhook event.

The backup command uses SQLite's online backup API, so it creates a consistent snapshot while the worker is active. Back up `WORKER_DATABASE_PATH` regularly to a separate location, retain multiple known-good snapshots, and verify a backup by opening it with SQLite or running the worker status command against it. Restore by stopping the receiver and worker, replacing the database with a verified snapshot, and restarting both services. Any jobs received after the snapshot and before failure must be redelivered by GitLab or recovered from its webhook retry history.

`/health` only reports that the API process is alive. `/ready` checks that the configured queue database can be initialized and reports worker enablement; it does not call GitLab or AI providers.

The worker handles SIGINT and SIGTERM by stopping new claims, allowing active jobs to finish, and exiting cleanly. Unexpected termination is recovered through job leases on the next worker startup.

## Security model

GitLab webhook signatures are verified against the raw request body using the configured signing token, timestamp tolerance, and constant-time comparison before event persistence. `webhook-id` is durably unique, so replay protection survives process restarts within the configured retention window.

All GitLab repository content is untrusted data. Prompt instructions remain authoritative, the AI has no tools, and AI output can only become validated MR description/note text through fixed GitLab client methods. The agent never executes repository code, shell commands, arbitrary URLs, approval, merge, branch writes, user administration, or project-permission changes.

The GitLab token should belong to `gi_ai_code_reviewer` and require only project/group access sufficient to read project and MR data, read changes/commits/approvals, update MR descriptions, and create/update MR notes. Administrator privileges are not required. The GitLab client exposes only those fixed read/write operations; repository content cannot select an endpoint.

Diffs sent to AI providers are filtered, size-limited, and redacted for common credential patterns. AI findings are validated for safe relative file references, bounded fields, supported severities, and positive integer line numbers. Provider errors and GitLab API errors do not include raw response bodies or credentials in external errors.

The queue database and online backups must be stored outside any web-served directory with owner-only permissions on Linux (`0700` directory, `0600` files). Configure firewall rules so port 8000 is reachable only by the GitLab host or required reverse proxy. HTTPS/TLS should be provided by a controlled internal reverse proxy before exposing the service beyond the trusted network.

For future systemd deployment, run receiver and worker under a dedicated unprivileged Linux user with access only to the application, queue database, restricted backup directory, and required network destinations. Use restart limits, log rotation, and no shell privileges beyond service startup.

## Testing webhook

Webhook handling is not implemented in this Phase 1 milestone. Once the webhook layer is added, it will be tested using a signed GitLab request.

## Testing OpenRouter

OpenRouter connectivity is not implemented in this Phase 1 milestone. In later phases, a dedicated connectivity check will be included to validate the API key and model configuration.

## Adding another project

This service is designed to be central and reusable across multiple GitLab projects rather than project-specific. Project-specific configuration will be introduced in later phases using a configuration directory such as:

```text
config/projects/
```

with per-project YAML settings that override global defaults.

## Project-specific configuration

This initial service does not yet include project configuration files. The project configuration system will be added in a later phase, with a global default layer and per-project overrides.

## Security considerations

- Keep API keys and secrets in `.env` only.
- Never log the GitLab token, webhook secret, or OpenRouter key.
- Do not commit `.env` files.
- Restrict network exposure of the service.
- Use a dedicated service account rather than an administrator token.
- Only update the AI-managed section inside designated markers in MR descriptions.

## Troubleshooting

- If the app does not start, verify the virtual environment is active and requirements are installed.
- If the service fails to bind to the port, ensure the configured port is free.
- If `.env` values are not loaded, confirm the file exists in the project root and is readable.
- If you see import errors, reinstall dependencies with `pip install -r requirements.txt`.

## Production deployment (Phase 6)

This repository is prepared for a single-server Linux deployment without changing the core architecture. The receiver and worker remain separate processes, SQLite remains the durable queue, and the application continues to read configuration from environment variables rather than hardcoded Linux paths.

Production deployment guidance and service templates are documented in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). The repository also includes templates under [deploy/systemd](deploy/systemd).

### Deployment constraints

- one Linux server, GitLab and the AI agent on the same network
- separate receiver and worker systemd units
- SQLite queue persisted outside the repo under a service-owned directory
- service user with minimal Linux permissions
- environment file stored outside Git under `/etc/git-ai-reviewer/agent.env` or a similar service-protected path
- backup and retention jobs managed by systemd timers or cron
- receiver and worker restart behavior documented and recoverable

### Phase status

- Phase 1: durable queue + worker
- Phase 2: operational reliability
- Phase 3: security hardening
- Phase 4: AI review quality + evaluation
- Phase 5: observability
- Phase 6: Linux production deployment templates and deployment documentation

## Phase status

This repository is currently at Phase 1 completion:

- FastAPI service scaffolded
- configuration loader implemented
- health endpoint implemented
- `.env.example` created
- requirements file created
- project README added

Future phases will add:

1. OpenRouter client and tests
2. GitLab client and MR retrieval
3. GitLab webhook endpoint
4. validation engine
5. AI MR summary generation
6. AI code review workflow
7. human approval checks
8. full MR processing integration
