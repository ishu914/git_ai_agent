# Linux deployment guide for the GitLab AI reviewer

This guide documents the safe single-server Linux deployment model for the GitLab AI review service. It is intentionally scoped to the current architecture: a FastAPI receiver, a separate worker process, SQLite durable storage, and environment-driven configuration.

## 1. Deployment target and constraints

- Linux target: Ubuntu 24.04
- Python: 3.12.x
- Single server model: GitLab and AI agent share the same private network/host
- Application source is version-controlled on Windows and pulled to Linux via Git
- Real secrets remain outside the repository in a service-owned environment file
- No PostgreSQL migration, no Kubernetes, no dashboard stack, and no new AI capabilities are introduced in this phase

## 2. Production filesystem layout

Example layout:

```text
/opt/git-ai-reviewer/
    application/
    venv/

/var/lib/git-ai-reviewer/
    jobs.sqlite3

/var/backups/git-ai-reviewer/
    jobs.sqlite3.backup
    jobs.sqlite3.backup.YYYYMMDD-HHMMSS

/etc/git-ai-reviewer/
    agent.env

/var/log/git-ai-reviewer/
    (only if file-based logging is explicitly configured)
```

Recommended ownership and permissions:

```bash
sudo mkdir -p /opt/git-ai-reviewer/application /opt/git-ai-reviewer/venv \
  /var/lib/git-ai-reviewer /var/backups/git-ai-reviewer /etc/git-ai-reviewer /var/log/git-ai-reviewer

sudo chown -R git-ai-reviewer:git-ai-reviewer /opt/git-ai-reviewer /var/lib/git-ai-reviewer /var/backups/git-ai-reviewer /var/log/git-ai-reviewer
sudo chmod 700 /etc/git-ai-reviewer /var/lib/git-ai-reviewer /var/backups/git-ai-reviewer /var/log/git-ai-reviewer
sudo chmod 600 /etc/git-ai-reviewer/agent.env
sudo chmod 700 /opt/git-ai-reviewer /opt/git-ai-reviewer/application /opt/git-ai-reviewer/venv
```

The exact production paths are configurable; the repository must not assume `/home/sam/git_ai_agent` is the deployment location.

## 3. Service user

Create a dedicated Linux service account:

```bash
sudo useradd --system --home /opt/git-ai-reviewer --no-create-home --shell /usr/sbin/nologin git-ai-reviewer
```

Requirements:

- no interactive login
- no shell access
- no root privileges
- access limited to required application directory, database directory, backup directory, and log directory
- no access to unrelated files or directories

This user should own the app tree, the SQLite database, and the backup/log directories used by the service.

## 4. Application installation

On the Linux host, clone or pull the repository into the stable application path:

```bash
sudo git clone https://git.example.com/git-ai-reviewer.git /opt/git-ai-reviewer/application
```

or, for updates:

```bash
cd /opt/git-ai-reviewer/application
git pull
```

The Linux host should not require manual editing of application files after deployment. The code is version-controlled and deployed by Git.

## 5. Python environment

Create and populate a dedicated virtual environment:

```bash
sudo python3.12 -m venv /opt/git-ai-reviewer/venv
sudo /opt/git-ai-reviewer/venv/bin/pip install --upgrade pip
sudo /opt/git-ai-reviewer/venv/bin/pip install -r /opt/git-ai-reviewer/application/requirements.txt
```

Then verify the environment:

```bash
/opt/git-ai-reviewer/venv/bin/python -V
/opt/git-ai-reviewer/venv/bin/python -m pytest -q
```

Do not rely on global Python packages. Dependency changes must be deliberate and validated in the same venv.

## 6. Environment file

Create a service-owned environment file outside Git:

```bash
sudo install -d -o root -g root -m 700 /etc/git-ai-reviewer
sudo touch /etc/git-ai-reviewer/agent.env
sudo chown root:root /etc/git-ai-reviewer/agent.env
sudo chmod 600 /etc/git-ai-reviewer/agent.env
```

Populate it with the required environment variables, for example:

```bash
GITLAB_URL=http://192.168.2.86
GITLAB_TOKEN=...
GITLAB_AI_USERNAME=gi_ai_code_reviewer
GITLAB_WEBHOOK_SIGNING_TOKEN=whsec_...
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=openai/gpt-4o-mini
OPENROUTER_ENABLED=true
GROQ_API_KEY=...
GROQ_ENABLED=true
WORKER_DATABASE_PATH=/var/lib/git-ai-reviewer/jobs.sqlite3
AI_AGENT_HOST=0.0.0.0
AI_AGENT_PORT=8000
WORKER_ENABLED=true
WORKER_CONCURRENCY=2
AI_MAX_CONCURRENT_REVIEWS=2
WORKER_MAX_ATTEMPTS=3
WORKER_LEASE_SECONDS=900
WORKER_JOB_RETENTION_DAYS=90
```

Notes:

- This file is not tracked in Git.
- It must not be web-accessible.
- It must not be logged by the application.
- It must not be copied into the repo or committed.

## 7. systemd service installation

The repo includes service templates under [deploy/systemd](../deploy/systemd). Copy them to `/etc/systemd/system/` on the Linux host and review the paths before enabling them.

Example:

```bash
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-web.service /etc/systemd/system/
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-worker.service /etc/systemd/system/
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-backup.service /etc/systemd/system/
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-backup.timer /etc/systemd/system/
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-retention.service /etc/systemd/system/
sudo cp /opt/git-ai-reviewer/application/deploy/systemd/git-ai-retention.timer /etc/systemd/system/
```

Then reload and enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now git-ai-web.service
sudo systemctl enable --now git-ai-worker.service
sudo systemctl enable --now git-ai-backup.timer
sudo systemctl enable --now git-ai-retention.timer
```

## 8. Service behavior

### Receiver service

The web receiver is responsible for:

- accepting GitLab webhook requests
- validating signature/timestamps
- rejecting irrelevant or duplicate events
- creating durable SQLite jobs
- serving `/health`, `/ready`, and `/metrics`

It must not run long AI analysis or block on external provider calls.

### Worker service

The worker is responsible for:

- claiming queue items
- recovering expired leases
- processing GitLab MR state
- running deterministic validation
- executing AI review if appropriate
- updating MR notes and description sections
- retrying transient failures
- preserving dead-letter records for failed jobs

The worker depends on the durable queue, not on in-memory state from the receiver.

## 9. systemd hardening

The provided unit templates use conservative process hardening where compatible with the application:

- `NoNewPrivileges=true` — blocks privilege escalation
- `PrivateTmp=true` — prevents temp-directory leakage
- `ProtectSystem=strict` — makes the filesystem read-only outside the explicitly writable paths
- `ProtectHome=true` — prevents unintended access to user home directories
- `ReadWritePaths=/var/lib/git-ai-reviewer /var/log/git-ai-reviewer` — allows the service to write to required application data directories
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` — limits networking to standard IP and Unix sockets
- `LimitNOFILE=65535` — allows a healthy number of file descriptors for the web service and worker

These settings are intentionally limited to what the service requires. They do not disable GitLab access, SQLite access, or required outbound provider connectivity.

## 10. SQLite deployment model

The queue uses SQLite with WAL mode and durable transactional writes. The deployment must preserve that model.

Production requirements:

- database stored under `/var/lib/git-ai-reviewer` or another service-owned directory
- parent directory owner-only permissions
- database file owner-only permissions
- WAL mode remains enabled
- database is not inside the repository
- database is not web-accessible
- backups are stored under `/var/backups/git-ai-reviewer` and kept separate from the application directory

## 11. Backup and restore

The project already includes backup capability:

```bash
python -m app.worker.runner --backup /var/backups/git-ai-reviewer/jobs.sqlite3.backup
```

Recommended scheduling model:

- systemd timer is preferred because it is explicit and easy to manage on a single Linux server
- keep backups in a separate backup directory with restrictive permissions
- fail visibly if a backup command fails
- retain multiple valid snapshots and do not delete backups blindly
- test a restore process in a non-production copy before relying on it for recovery

Example timer schedule: daily backup, weekly retention cleanup.

## 12. Retention cleanup

The project provides:

```bash
python -m app.worker.runner --cleanup-retention
```

This must be scheduled externally and must not run on every webhook request. It preserves the Phase 2 behavior: completed, obsolete, or dead-letter jobs older than the configured retention window are deleted; active, pending, processing, and retryable jobs remain intact.

## 13. Logging and rotation

The project uses structured logging via `app.observability`. The simplest and most compatible Linux model is journald-backed logging via systemd. That keeps log capture with the service and avoids the maintenance burden of another logging system.

If journald is used:

- set storage to a bounded size under systemd journal settings
- use a retention policy suitable for a single-server deployment
- ensure secrets remain redacted from log output
- keep logs readable by the service account only if file-based logs are used

If file-based logging is introduced later, configure logrotate to rotate and compress logs while preserving restricted permissions.

## 14. Network binding and firewall

The service currently uses `uvicorn app.main:app --host 0.0.0.0 --port 8000` in the default config. In a single-server internal deployment, it is often reasonable to keep the listener bound to `0.0.0.0` only if the firewall restricts access to the GitLab server or required reverse proxy.

Recommended firewall posture:

- allow inbound port `8000` only from the GitLab host or an approved reverse proxy
- keep `/webhook/gitlab` accessible only to trusted clients
- restrict `/metrics` and `/health` to trusted internal systems if remote visibility is not needed
- allow outbound egress to GitLab, OpenRouter, and Groq as required by the environment
- avoid opening port `8000` to the entire network

This is a deployment policy decision and must be enforced by the Linux host firewall.

## 15. Internal TLS and reverse proxy

The current setup uses `http://192.168.2.86`, matching the current internal GitLab URL. This does not require a public TLS redesign during the current phase.

A future internal TLS option can sit in front of the AI agent, but it must not break webhook signature verification. If a reverse proxy terminates TLS, the proxy must preserve the raw request body and the webhook signing headers at the time the service validates the signature.

## 16. Health, readiness, and metrics exposure

The application exposes:

- `/health`
- `/ready`
- `/metrics`

These endpoints should remain internal and not be made public without clear reason. They must not include secrets, API keys, raw GitLab content, or webhook signing tokens.

Operational meaning:

- `/health`: process is up
- `/ready`: queue store can initialize and the service is configured to run
- `/metrics`: structured counters and gauges without secret-bearing payloads

## 17. Safe startup order

The startup sequence should be:

1. validate configuration
2. ensure database directory is present and writable
3. start receiver service
4. start worker service
5. allow worker to recover expired leases on startup
6. resume queue processing

The receiver must not depend on the worker being alive in order to accept and persist valid webhook jobs. The worker must recover pending jobs after a restart.

## 18. Restart and graceful shutdown behavior

The worker already includes graceful shutdown logic with SIGINT/SIGTERM handling. The Linux deployment should preserve this behavior:

- service should stop accepting new claims before exiting
- active jobs should finish if possible
- the worker should exit cleanly
- systemd should use a reasonable stop timeout rather than force-killing the process immediately

The receiver can restart independently. The worker restart must recover queued jobs and expired leases without data loss.

## 19. Update and rollback workflow

### Standard update workflow

On Windows:

```bash
# code changes, tests, commit, push
python -m pytest -q
git add .
git commit -m "Update deployment configuration"
git push
```

On Linux:

```bash
cd /opt/git-ai-reviewer/application
git pull
/opt/git-ai-reviewer/venv/bin/python -m pytest -q
sudo systemctl restart git-ai-web.service
sudo systemctl restart git-ai-worker.service
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/metrics
```

### Rollback workflow

- identify the last known-good Git commit
- stop or restart the services if necessary
- check out the previous known-good version
- reinstall dependencies if the environment changed
- verify health/readiness and queue behavior
- avoid rolling back the SQLite schema unless a tested rollback path exists

Database migrations are intentionally kept minimal and compatibility-focused where practical.

## 20. Smoke test plan

Use a dedicated test GitLab project/MR. Do not use production-sensitive repositories for the first deployment test.

1. GET `/health`
2. GET `/ready`
3. GET `/metrics`
4. inspect worker status
5. inspect queue status
6. send one GitLab MR event
7. verify the webhook is accepted
8. verify a job is created in SQLite
9. verify the worker claims the job
10. verify AI processing runs or returns an explicit unavailable status
11. verify MR description update/notes behavior
12. verify job reaches `COMPLETED`
13. verify metrics increment and logs remain structured
14. verify no secret values appear in logs

## 21. Failure tests to document before production

Before production deployment, the operator should test or at least simulate:

- worker restart recovery
- receiver restart recovery
- temporary provider failure
- temporary GitLab failure
- duplicate webhook rejection
- expired lease recovery
- dead-letter job handling
- backup failure behavior
- database unavailable behavior
- disk-space alerting and backup failures

These tests must be done against a non-production project or isolated environment.

## 22. Dependency security and CI

The project still has dependency vulnerability scanning as remaining work. The goal is deliberate review of known issues, not blanket upgrades. If GitHub Actions is in use, integrate the scan into the existing CI flow rather than creating a separate deployment-heavy system.

## 23. Production checklist

Before considering the deployment operationally ready:

- service user created
- directories created with correct ownership and permissions
- environment file exists outside Git and has restricted permissions
- Python venv created and dependencies installed from `requirements.txt`
- systemd units reviewed and installed
- database directory is writable by the service user
- backup path is separate from the application directory
- queue and worker restart behavior validated
- health/readiness/metrics endpoints working
- GitLab webhook signature validation remains intact
- no secrets logged
- smoke tests pass with a dedicated test MR project

## 24. Important Linux verification items

The following items must be verified on the actual Linux host and are deliberately not assumed by the repository itself:

- systemd unit syntax and service boot behavior
- real file ownership and permissions
- firewall rules for port 8000
- journald or logrotate behavior
- backup retention/troubleshooting results
- final GitLab webhook reachability
- provider connectivity from the Linux server
- actual service startup order and restart recovery

This deployment work is complete only when those Linux-specific checks pass on the target environment.
