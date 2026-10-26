# Agent Guide

## Purpose and architecture

This is the public, multi-app `matrix2669/home-assistant-addons` repository. `ipv6_proxy_monitor/` is a standalone Home Assistant app, not a HACS companion. The app periodically resolves one configured hostname's AAAA records, maps answers to the configured IPv6 network prefix, and presents a persistent notification linking to a native Ingress review page. The review page requires an authenticated HA administrator to approve a fresh candidate. Only an explicit POST approval calls the Home Assistant HTTP configuration WebSocket API. Home Assistant owns pending/revert semantics and restarts itself when its HTTP configuration changes.

The app uses Supervisor's authenticated Home Assistant WebSocket connection (`homeassistant_api: true`) and local Supervisor HTTP endpoints. It stores only noncredential transaction state atomically below `/data`. It does not mount Home Assistant configuration, read `.storage`, use a user-supplied token, change YAML, call internal storage/promote commands, restart HA directly, or confirm a pending change.

## Workspace Standards Reconciliation Gate

Routine feature/fix work uses the applied local baseline recorded in `WORKSPACE-STANDARDS.yaml`. Bootstrap/adoption, explicit standards work, and pre-promotion/release require current reconciliation: verify workspace Git and standards sources, run `scripts/reconcile-standards --check` and `--diff`, review this guide, decisions, dependencies, source, branch/release policy and related contracts, resolve conflicts, then apply only after review. Missing workspace history or material history sources makes bootstrap preliminary; never claim complete history review. A contradiction blocks promotion; ask focused questions rather than choosing silently.

## Non-negotiable safety and security

- No HTTP change is made by polling, Ingress GET, notification navigation, startup recovery, or a retry. Mutation requires a fresh, explicit, authenticated administrator POST approval.
- Never use forwarded headers to identify the socket peer. Accept Ingress only from the Supervisor proxy peer `172.30.32.2`; require a valid `X-Remote-User-Id` and independently verify it is an HA administrator.
- Bind port 8099 internally and expose no host port. Use relative links/forms so dynamic `X-Ingress-Path` works. Escape all untrusted values. Never put the Ingress token in notifications or logs.
- Polling is read-only with respect to approval state; only an authenticated Ingress GET creates/replaces a preview, and no polling/recovery action may erase an active transaction. Serialize poll, preview, apply and recovery through one coordinator lock.
- Bind approval to a random user-bound nonce, DNS candidate, exact normalized stable HTTP config fingerprint and active config slot. Immediately recheck before configure. Persist only hashes, network identity, transaction IDs and prior ownership state; do not persist the HTTP config or credential values.
- Persist intent before configure. On reconnect, compare complete source/candidate hashes and HA active config slot; never infer success merely because a proxy entry is present. Clear resolved transaction fields and preserve previous ownership on rollback/external edits. Never blindly retry uncertain configure calls.
- Existing matching entries not previously managed by this app remain unmanaged; this version does not adopt them. Never silently change `use_x_forwarded_for`; preserve all HTTP fields except the explicit proxy-list change. Distinct resulting IPv6 networks block the action; no arbitrary selection.
- Enforce minimum HA 2026.10.0 in every new WebSocket session, with bounded connect/receive deadlines and session reset on failure. DNS failures, empty/invalid answers, unsupported versions, failed pending HA config, or ambiguous state leave HA unchanged. Failed pending config stays for operator inspection and blocks app writes until resolved in HA. No automatic confirmation.
- HA does not provide an atomic compare-and-swap for HTTP configure. Document the remaining race; do not claim transaction-level concurrency guarantees beyond app serialization and immediate rechecks.
- Do not install, deploy, publish images/releases, promote branches, or mutate a live HA instance without separate explicit authorization.

## Development and tests

Use Python 3.12, pinned aiohttp/dnspython requirements and a digest-pinned multi-architecture Python image. Keep DNS, HA API, notifications, sessions, options, storage paths, clocks and peer checks injectable. Tests use local fake HTTP/WebSocket servers and never connect to live HA. Run the full unittest suite, manifest parse/schema checks, `compileall`, workspace standards check and `validate-project`. Docker/build validation must be reported unavailable if not run. Do not claim production readiness before real Ingress/restart/rollback validation.

## Branch and release workflow

Use `main` for stable source/releases and `dev` for next-release integration; feature/fix branches start from `dev` after that branch exists. This initial feature branch is based on `main` because `dev` did not exist at bootstrap. Repository-change authorization normally covers checkpoint commits/pushes to the isolated work branch under workspace standards; it never implies promotion, release, image publication, installation, or deployment. Follow `BRANCHES.md`, `CHANGELOG.md`, `RELEASE.md`, and `VERSION`.

## Bootstrap evidence limitations

See `docs/BOOTSTRAP-HISTORY.md`. The parent task supplied an evidence summary, but no ChatGPT/Codex history enumeration/read tool was available. Treat this bootstrap as preliminary, not a complete historical review.
