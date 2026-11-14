# Security

This source preview has not completed full release validation or a full
security audit. See [NOTICE.md](NOTICE.md) for licensing scope.

## Safe operation

- Keep credentials, provider configuration, database files, browser profiles,
  project files, memory, conversations and backups outside version control.
- Initialize a new data home for each evaluation. Never point a test at a live
  business home or restore a production backup into a public example.
- Bind the console to loopback by default. Account setup, access control and
  network exposure require an explicit operator decision.
- Review tool permissions, external-agent launch settings, model providers and
  data flows before enabling execution. Local installation does not make cloud
  model requests or outbound tools local.
- An approval or cancellation record does not undo an external operation that
  already completed. Inspect the actual result before retrying a task.
- The UI entry point can install missing dependencies or rebuild its frontend.
  Prepare dependencies explicitly before treating startup as an isolated check.

## Reporting a vulnerability

Do not post API keys, tokens, passwords, customer data or live exploit evidence in
public issues. If the repository provides GitHub private vulnerability reporting,
use its **Security → Report a vulnerability** entry. If that entry is unavailable,
request a private reporting channel without disclosing the vulnerability details.
No private reporting service or response-time commitment is claimed by this draft.

When sharing a report, use synthetic data and include the affected version,
preconditions and a minimal reproduction. If a credential was exposed, its owner
should revoke or rotate it; removing it from a file does not revoke the credential
or remove copies already obtained by other people.
