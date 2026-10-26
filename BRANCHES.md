# Branches

Git hosting is authoritative for remote refs; this is the local intent record.

| Branch | Type | Status | Base | Target | Purpose |
|---|---|---|---|---|---|
| `main` | long-lived | active | — | stable releases | Production-ready releases. |
| `dev` | long-lived | planned | `main` | `main` | Integrates the next release. |
| `feature/ipv6-proxy-monitor` | feature | active | `main` at `68e0c485db6c83724ea3936a6a4c1851796f4659` | planned `dev` | Build the standalone IPv6 Proxy Monitor app and its offline safety tests. |

## `feature/ipv6-proxy-monitor`

- Purpose: publish initial source for an independently installed Home Assistant app that detects a changed IPv6 trusted-proxy network and asks an administrator before configuring HTTP settings.
- Scope: app source, tests, docs, governance, and local CI only.
- Project scope: source, tests, docs, governance, and local CI. An approved isolated feature-branch checkpoint commit/push is permitted; this does not authorize installation, deployment, live Home Assistant mutation, releases, promotion, or image publishing.
- Checkpoint status: prepared for the first checkpoint commit/push on base `68e0c485db6c83724ea3936a6a4c1851796f4659`; no checkpoint has yet been created or pushed. Do not record this branch's future checkpoint head here (it would make the record self-referential).
- Remote status: authenticated GitHub metadata observed only `main` at the base commit before the first checkpoint, on 2026-10-26. Remote metadata may advance after this observation.
- Validation (2026-10-26): offline unittest 24/24 passed, including uncertain-write recovery, notification retention, local fake HTTP/WebSocket runtime tests, and YAML manifest/default/schema checks; `compileall`, `git diff --check`, workspace `reconcile-standards --check`, and `validate-project` passed. Test venv used aiohttp 3.14.5, dnspython 2.9.0, and PyYAML 6.0.3. Docker build unavailable (`docker` is not installed); HA/Supervisor live integration was not run. Bootstrap history remains preliminary because prior AI histories were unavailable.
- Last validation update: 2026-10-26.
- Risks: administration-level change to Home Assistant HTTP settings; no atomic conditional configure API is available.
- Outcome: experimental source tree accepted by an independent final reviewer; no PR, release, installation, or deployment authorized.
