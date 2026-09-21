# Phase 7 — GitLab and AI Provider Failure Testing

## Scope

This report records only the Phase 7 failure and recovery checks actually executed in this workspace.

## Scenarios tested

- GitLab client connection timeout
- GitLab client connection error
- GitLab API retryable status handling for 429/500/502/503
- GitLab API permanent failure handling for 401/403/404/409
- malformed GitLab API JSON
- missing GitLab file payload fields
- raw GitLab response body and token sanitization checks
- OpenRouter timeout and connection error handling
- OpenRouter 429 quota handling
- empty AI output detection
- malformed JSON detection
- incomplete finish_reason=length handling
- provider fallback and global attempt-budget enforcement
- provider/model cooldown behavior
- model-discovery filtering for unusable audio/embedding models
- queue retry-wait/backoff and expired lease recovery
- webhook rejection and queue safety checks

## Test counts

- Total targeted tests run: 60
- Passed: 60
- Failed: 0
- Skipped: 0
- Warnings: 2 dependency deprecation warnings

## Notes

- Live OpenRouter/Groq provider calls were NOT performed.
- This Phase 7 slice remains scoped to deterministic offline testing only.
- No production readiness claim is made.
- The current evidence only covers the tested offline scenarios above.

## Phase 7 — Security Abuse & Untrusted Input Testing

### Scope

This section records the verified security-abuse checks completed in the current workspace.

### Attack classes tested

- webhook signing and timestamp abuse
- prompt-injection content in user-controlled MR text and payloads
- secret leakage in diffs, logs, and metadata
- path traversal and unsafe file references in AI findings
- invalid-finding normalization and deduplication
- GitLab permission boundary checks
- metrics/logging sanitization and bounded output

### Test counts

- Total security-abuse tests run: 15
- Passed: 15
- Failed: 0
- Skipped: 0
- Warnings: 1 dependency deprecation warning

### Defects found and fixed

- Redaction coverage was widened to catch bearer-token and API-key-like values that were previously left in sanitized payload output.
- File-path normalization was tightened to reject absolute, drive-letter, traversal, and malformed repo paths before findings are accepted.

### Security invariants verified

- untrusted repository content is treated as data, not authority
- malformed or exploitative file paths are rejected
- secret-bearing strings are masked from payloads and logs
- duplicate and invalid findings do not pass validation
- GitLab client methods remain limited to the application’s expected read/write surface
- metrics and logs do not expose tokens or raw source/diff content

### Known limitations

- Live AI/provider calls were not performed.
- This report reflects the verified offline security subset only.
- Remaining Phase 7 categories include SQLite/backup/restore, concurrency/load, observability, and final recovery testing.

## Phase 7 — SQLite, Backup, Restore & Retention Testing

### Scope

This section records the verified SQLite durability and backup/restore checks completed for the current workspace.

### Scenarios tested

- SQLite schema initialization and WAL mode checks
- required table/index validation
- busy timeout and foreign-key configuration checks
- transaction rollback under forced write failure
- concurrent same-job claim behavior
- concurrent multi-job claim behavior
- temporary SQLite lock/contention stability
- I/O/unavailable database path behavior
- SQLite backup creation and integrity validation
- backup while database activity is ongoing
- restore into a separate database and integrity validation
- retention cleanup with active jobs protected
- backup snapshot validity after retention

### Test counts

- Total SQLite/backup/restore tests run: 12
- Passed: 12
- Failed: 0
- Skipped: 0
- Warnings: 1 dependency deprecation warning

### Defects found and fixed

- The initialization test was aligned to the project’s actual connection contract: foreign keys and busyness are enforced on the app-owned SQLite connection object, not a raw connection opened outside the app.
- The retention safety test was corrected to assert against an actually active processing job rather than a historical row that had already been selected by the queue.

### Invariants verified

- job creation and claim transitions remain durable and consistent
- locked or contended SQLite access does not silently corrupt queue state
- backups remain readable and integrity-check clean
- restores preserve the expected queue state in a separate database
- retention removes only historical jobs while leaving active jobs intact

### Linux-only / offline limitations

- Linux filesystem permissions and systemd backup scheduling were not executed on a real Linux target host in this workspace.
- This report covers deterministic offline verification only and does not claim production Linux deployment has been validated.

## Known limitations

- This document reflects the verified subset only; it does not claim complete production validation.
- Full live-provider testing remains out of scope for this checkpoint.
- Remaining Phase 7 categories include concurrency/load testing, observability testing, and final recovery/integration testing.
