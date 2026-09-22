# GitLab AI Agent Production Deployment

This is the active deployment model:

- one Linux server
- one FastAPI/Uvicorn process
- one systemd service: `git-ai-agent.service`
- no worker process, queue database, backup timer, or second application service

## Runtime flow

```text
GitLab System Hook -> FastAPI -> in-process MR task -> GitLab MR/diff
  -> deterministic validation -> AI orchestration/fallback
  -> controlled MR description section and AI review note
```

The process has no durable queue. In-flight work is lost if the process exits; systemd restarts the service and GitLab may redeliver webhook events according to its own retry policy.

## Prerequisites

- Ubuntu or another supported Linux distribution
- Python 3.12 with venv support
- network access to GitLab and configured AI providers
- a dedicated unprivileged account such as `git-ai-reviewer`
- a GitLab service account named `gi_ai_code_reviewer` with only the required project/MR permissions

## Filesystem and service user

```bash
sudo useradd --system --home /opt/git-ai-reviewer --no-create-home --shell /usr/sbin/nologin git-ai-reviewer
sudo install -d -o git-ai-reviewer -g git-ai-reviewer -m 750 /opt/git-ai-reviewer
sudo install -d -o git-ai-reviewer -g git-ai-reviewer -m 750 /opt/git-ai-reviewer/application
sudo install -d -o git-ai-reviewer -g git-ai-reviewer -m 750 /opt/git-ai-reviewer/venv
sudo install -d -o root -g root -m 700 /etc/git-ai-reviewer
sudo install -d -o git-ai-reviewer -g git-ai-reviewer -m 750 /var/log/git-ai-reviewer
sudo install -o root -g root -m 600 /dev/null /etc/git-ai-reviewer/agent.env
```

The application user needs read access to the application, virtual environment, and environment file, plus write access only to the configured log directory. The environment file must not be stored in the repository.

## Install the application

```bash
sudo git clone <repository-url> /opt/git-ai-reviewer/application
sudo python3.12 -m venv /opt/git-ai-reviewer/venv
sudo /opt/git-ai-reviewer/venv/bin/pip install --upgrade pip
sudo /opt/git-ai-reviewer/venv/bin/pip install -r /opt/git-ai-reviewer/application/requirements.txt
sudo chown -R git-ai-reviewer:git-ai-reviewer /opt/git-ai-reviewer
```

## Environment and secrets

Populate `/etc/git-ai-reviewer/agent.env` with the required values:

```text
GITLAB_URL=http://gitlab.internal
GITLAB_TOKEN=<service-account-token>
GITLAB_AI_USERNAME=gi_ai_code_reviewer
GITLAB_WEBHOOK_SIGNING_TOKEN=whsec_<signing-token>
OPENROUTER_API_KEY=<optional-authorized-key>
OPENROUTER_MODEL=<configured-model>
OPENROUTER_ENABLED=true
GROQ_API_KEY=<optional-authorized-key>
GROQ_ENABLED=true
AI_AGENT_HOST=0.0.0.0
AI_AGENT_PORT=8000
```

```bash
sudo chown root:root /etc/git-ai-reviewer/agent.env
sudo chmod 600 /etc/git-ai-reviewer/agent.env
```

Never put tokens in the unit file, source, YAML, README, tests, logs, or Git. Do not print the environment file during diagnostics. Optional providers such as AECO_AI must not make startup fail when absent.

## systemd installation

Install [deploy/systemd/git-ai-agent.service](../deploy/systemd/git-ai-agent.service):

```bash
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now git-ai-agent.service
```

The unit directly executes the production virtual environment; activation is not required:

```text
/opt/git-ai-reviewer/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

It uses `Restart=on-failure` with a five-second delay, starts after `network-online.target`, runs as `git-ai-reviewer`, and does not configure multiple Uvicorn workers.

The unit uses `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `ProtectHome`, `RestrictSUIDSGID`, `RestrictNamespaces`, `LockPersonality`, `TasksMax=64`, and `MemoryMax=1G`. Outbound IPv4/IPv6/Unix networking remains allowed for GitLab and AI provider access. Adjust the memory limit only after observing real workload behavior.

## Service operations

```bash
sudo systemctl status git-ai-agent.service
sudo systemctl restart git-ai-agent.service
sudo systemctl stop git-ai-agent.service
sudo systemctl start git-ai-agent.service
journalctl -u git-ai-agent.service -f
journalctl -u git-ai-agent.service --no-pager -n 100
```

Uvicorn receives normal systemd termination signals. There is no durable queue or crash recovery for in-flight MR work.

## GitLab System Hook

Configure the GitLab System Hook for Merge Request events at:

```text
https://<internal-host>/webhook/gitlab
```

Configure the same signing token in `GITLAB_WEBHOOK_SIGNING_TOKEN`. The endpoint validates the raw body, webhook ID, signature, and timestamp before scheduling an in-process task. Invalid or stale events must not reach AI processing.

## Health, readiness, and metrics

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/metrics
```

`/health` checks process liveness without calling AI. `/ready` reports the single-process runtime state. `/metrics` exposes bounded operational counters without secrets, source, complete diffs, prompts, or raw provider responses.

## Logging and failure handling

Structured logs identify webhook receipt/acceptance, processing stages, provider/model selection, fallbacks, GitLab failures, AI failures, description updates, review notes, and commit-message preservation/generation. Secret values, authorization headers, complete sensitive diffs, and credentials must remain redacted.

GitLab and AI timeouts are bounded by the existing 30-second client timeouts and retry limits. Provider failures are isolated to the MR task; no fake review is written. GitLab failures are reported safely and do not grant approval, merge, push, branch, user, or permission capabilities.

Journald is the logging mechanism. Configure host-level journal retention/size limits according to the server policy; do not add a second application logging service.

## Update procedure

```bash
cd /opt/git-ai-reviewer/application
git fetch --prune
git pull --ff-only
/opt/git-ai-reviewer/venv/bin/pip install -r requirements.txt
/opt/git-ai-reviewer/venv/bin/python -m pytest -q
sudo systemctl restart git-ai-agent.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

Perform a controlled smoke test after restart and inspect the journal for processing completion.

## Rollback procedure

```bash
cd /opt/git-ai-reviewer/application
git log --oneline -5
git checkout <known-good-commit>
/opt/git-ai-reviewer/venv/bin/python -m pytest -q
sudo systemctl restart git-ai-agent.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

Do not roll back by copying secrets or runtime files into the repository.

## Reboot and smoke-test procedure

On an approved non-production Linux host:

```bash
sudo systemctl enable --now git-ai-agent.service
systemctl is-active git-ai-agent.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
sudo reboot
```

After reboot:

```bash
systemctl is-active git-ai-agent.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/metrics
```

Then send one harmless controlled MR and verify the System Hook, in-process processing, deterministic validation, AI gate/provider, description update, review note, correct file statistics, no duplicate AI section, and no secret exposure.

## Concurrency and failure injection

Use a non-production project and mocks where possible. Trigger multiple webhook events close together and verify the HTTP process remains responsive, each MR remains isolated, and duplicate events do not create uncontrolled notes or sections. Exercise invalid signatures, malformed payloads, GitLab 401/403/404/5xx/timeouts, provider timeout/429/malformed/incomplete responses, and missing optional provider configuration without damaging production data.

## Filesystem hygiene

`.gitignore` excludes `.env`, virtual environments, pytest cache, SQLite/database files, logs, backups, and temporary files. Verify with Git on a checkout:

```bash
git status --short
git diff --check
```

If the deployment directory is not a Git checkout, record that fact rather than claiming Git hygiene was verified.

## Known limitations

- one application process only
- no durable queue or crash recovery for in-flight tasks
- live systemd, reboot, resource, and GitLab smoke tests must be executed on the target Linux server
- AI provider availability depends on authorized external credentials and network access
