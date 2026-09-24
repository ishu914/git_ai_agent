# GitLab AI Agent

Single-process GitLab AI review service for self-hosted GitLab installations.

## Architecture

The active model is intentionally simple and single-service:

GitLab System Hook
	-> FastAPI webhook endpoint
	-> durable SQLite event store
	-> in-process event dispatcher
	-> authoritative MR fetch and validation
	-> AI orchestration / fallback
	-> AI-managed MR description update and review note

The service runs as one FastAPI process. Accepted events are persisted to SQLite before acknowledgement; the in-process dispatcher is the only path that invokes MR processing. There is no separate worker process.

## Requirements

- Python 3.11+
- FastAPI
- Uvicorn
- Pydantic Settings
- GitLab access token and webhook signing secret

## Local setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create a local `.env` file based on `.env.example` and keep real credentials outside the repository.

## Runtime configuration

Key settings include:

```bash
GITLAB_URL=http://gitlab.internal
GITLAB_TOKEN=...
GITLAB_AI_USERNAME=gi_ai_code_reviewer
GITLAB_WEBHOOK_SIGNING_TOKEN=whsec_...

OPENROUTER_API_KEY=...
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_ENABLED=true

GROQ_API_KEY=...
GROQ_ENABLED=true

ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-...
ANTHROPIC_BASE_URL=https://api.anthropic.com
ANTHROPIC_ENABLED=false

OPENAI_API_KEY=...
OPENAI_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_ENABLED=false

OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=...
OLLAMA_ENABLED=false

AI_PROVIDER_PRIORITY=anthropic,openai,ollama,openrouter,groq

AI_AGENT_HOST=0.0.0.0
AI_AGENT_PORT=8000
```

The default provider order is Anthropic, OpenAI, Ollama, OpenRouter, then Groq. Disabled or incompletely configured providers are skipped without an attempted request. Anthropic and OpenAI base URLs support compatible internal gateways; Ollama needs a model and normally no key. The application requires at least one enabled, fully configured provider at startup and fails closed at the AI step without fabricating provider output.

## Run locally

Development command:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Alternative entry point:

```bash
python -m app.main
```

## Webhook flow

The webhook endpoint validates the GitLab signature and timestamp, rejects irrelevant or duplicate events, persists accepted events, and wakes the in-process dispatcher. The dispatcher atomically claims durable events and runs processing within the configured concurrency limit. This keeps the service single-process and avoids a second management daemon.

The SQLite event store uses WAL mode and a busy timeout. It records claim leases, retry scheduling, dead-letter state, retention cleanup, and online backups. `EVENT_MAX_QUEUE_DEPTH` bounds pending and retrying events; new unique deliveries receive HTTP 429 when the queue is full.

## AI review behavior

The processing pipeline:

- fetches the authoritative MR and diff from GitLab
- runs deterministic validation first
- compacts the diff into a token-budgeted payload
- selects a provider/model using the orchestrator fallback logic
- validates structured output before publishing results
- updates only the AI-managed MR description section
- creates or updates the AI review note

The orchestrator handles model discovery, provider fallback, cooldown, quota circuit breaking, token budgets, and secret masking.

### Review quality and commit messages

Review payloads include MR metadata, commit messages, deterministic file facts, diff statistics, and validation results without sending the repository wholesale. Python supplies file paths, additions, deletions, rename/binary/test classification, and diff size; AI explains engineering impact and reports only actionable findings.

Summaries distinguish evidence from unknown intent. Risk, change type, breaking-change status, testing assessment, reviewer attention, and review scope are normalized before the controlled AI section is rendered. Developer-written description text remains outside the `AI_REVIEW_START` and `AI_REVIEW_END` markers.

Commit-message generation is opt-in through `config/projects/default.yaml`. Meaningful user messages are preserved. AI may generate only for explicitly configured placeholder messages, and generated candidates are rejected if they contain secrets or fabricated issue identifiers. This feature selects a message; it never rewrites a GitLab commit.

## Security model

The service treats repository content as untrusted input and keeps the AI behind fixed, controlled GitLab client operations. It never automates approval, merge, push, branch writes, or permission changes.

The webhook layer validates the raw request body against the signing token, rejects stale timestamps, filters AI-generated events, and prevents duplicate processing of the same event key while the task is in-flight.

## Observability

The FastAPI app exposes:

- `/health`
- `/ready`
- `/metrics`

These report the live single-process runtime state. `/health` is independent of queued processing, while `/ready` verifies durable-store availability.

## Production deployment

Production uses one Linux service, one application process, and one environment file.

The single service runs:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Service template:

```ini
[Unit]
Description=GitLab AI Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=git-ai-reviewer
Group=git-ai-reviewer
WorkingDirectory=/opt/git-ai-reviewer/application
EnvironmentFile=/etc/git-ai-reviewer/agent.env
ExecStart=/opt/git-ai-reviewer/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start it with:

```bash
sudo systemctl enable --now git-ai-agent.service
sudo systemctl status git-ai-agent.service
journalctl -u git-ai-agent.service -f
```

## Testing

Run the repository test suite with the project virtual environment:

```bash
. .venv/bin/activate
python -m pytest -q
```

The current suite includes deterministic webhook, durable-event-store, security, AI fallback, observability, and end-to-end validation for the single-process model.

## Known limitation

The dispatcher remains in-process, so an operation interrupted during process shutdown can be retried after restart. Events left in processing state recover through their expired leases; completed and dead-letter events remain durable for retention and operational inspection.
