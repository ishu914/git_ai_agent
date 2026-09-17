# GitLab AI Agent

A central, reusable GitLab AI agent designed for self-hosted GitLab installations. This repository contains the initial Phase 1 foundation for the service: application bootstrap, settings loading, and a health check endpoint.

## Architecture

The end-state design is a central webhook-driven service that:

- receives GitLab Merge Request webhook events
- validates the signature and payload
- fetches MR metadata and diff from GitLab
- runs deterministic validation checks
- triggers AI review and summary generation through OpenRouter
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
GITLAB_WEBHOOK_SECRET=

OPENROUTER_API_KEY=
OPENROUTER_MODEL=

AI_AGENT_HOST=0.0.0.0
AI_AGENT_PORT=8000
```

### Notes

- Never commit the `.env` file.
- Keep API keys and secrets in environment variables only.
- The AI model must remain configurable through `OPENROUTER_MODEL`.

## OpenRouter configuration

This service is designed to use the OpenRouter API in OpenAI-compatible mode. The client should be configured via environment variables and should never hard-code credentials.

Planned configuration:

- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`
- timeout and retry settings via application configuration

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
