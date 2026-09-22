# GitLab AI Agent

Single-process GitLab AI review service for self-hosted GitLab installations.

## Architecture

The active model is intentionally simple and single-service:

GitLab System Hook
	-> FastAPI webhook endpoint
	-> in-process async task
	-> authoritative MR fetch and validation
	-> AI orchestration / fallback
	-> AI-managed MR description update and review note

There is no separate worker process and no durable SQLite queue in the active runtime model.

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

AI_AGENT_HOST=0.0.0.0
AI_AGENT_PORT=8000
```

The application validates the runtime configuration on startup and requires either OpenRouter or Groq credentials to reach the AI gate. If no live provider is configured, the system still exposes health/readiness and fails closed at the AI step without fabricating provider output.

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

The webhook endpoint validates the GitLab signature and timestamp, rejects irrelevant or duplicate events, and schedules processing through an in-process async task. This keeps the service single-process and avoids a second management daemon.

No queue database, worker lease, retry loop, or backup process is required in the current runtime contract.

## AI review behavior

The processing pipeline:

- fetches the authoritative MR and diff from GitLab
- runs deterministic validation first
- compacts the diff into a token-budgeted payload
- selects a provider/model using the orchestrator fallback logic
- validates structured output before publishing results
- updates only the AI-managed MR description section
- creates or updates the AI review note

The orchestrator still handles model discovery, provider fallback, cooldown, quota circuit breaking, token budgets, and secret masking. The worker does not own any of that logic.

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

These report the live single-process runtime state and do not pretend a queue or worker exists.

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

The current suite includes deterministic webhook, security, AI fallback, observability, and end-to-end validation for the single-process model.

## Known limitation

The current single-process design intentionally does not include a durable queue or crash recovery mechanism. If the FastAPI process exits, in-flight work is lost; the design assumes a single-service deployment with normal process supervision and restart.
