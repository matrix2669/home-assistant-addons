# Decisions

## ADR-001: Standalone app and explicit review workflow

- Status: Accepted; 2026-10-26.
- Decision: Build a standalone app in this public multi-app repository, first under `ipv6_proxy_monitor/`. Detect IPv6 network changes and notify an administrator with a relative internal Ingress link. The user reviews hostname, prefix, old and proposed networks, then explicitly chooses **Apply and restart**. The app does not automatically restart HA or confirm its pending settings. The administrator must manually confirm HA's HTTP settings within five minutes; HA rollback/restart behavior is authoritative.
- Reason: A destructive HTTP configuration change must remain visible and user-controlled. A separate HACS companion is out of scope.
- Alternatives rejected: automatic application/restart, companion integration, and direct YAML or `.storage` editing.
- Consequences: HA may restart only after a fresh explicit approval; review UI and durable state must survive that restart. No promotion call is ever made.
- Provenance: operator-approved project specification supplied 2026-10-26; initial repository commit `68e0c485db6c83724ea3936a6a4c1851796f4659`.

## ADR-002: DNS candidate selection

- Status: Accepted; 2026-10-26.
- Decision: Configure one hostname whose AAAA answers are parsed as IPv6 addresses and normalized with actual `ipaddress` bitwise network arithmetic (`strict=False`) at a configurable prefix length. Default prefix is `/128`; default polling cadence is five minutes. Host-bit-only address changes under a broader configured prefix do not trigger a network change. If answers collapse to multiple distinct networks, block and warn rather than selecting one. DNS errors, empty answers, or invalid answers do not change HA.
- Reason: Addresses can change inside a stable delegated prefix; text concatenation is not network arithmetic. Arbitrary selection from multiple networks risks trusting the wrong proxy.
- Alternatives rejected: `/128` only; deriving CIDR from address strings; picking first DNS result.
- Consequences: DNS resolution needs injectable testing; a valid changed candidate is not itself authorization.
- Provenance: operator-approved project specification supplied 2026-10-26.

## ADR-003: Managed proxy ownership and transaction safety

- Status: Accepted; 2026-10-26.
- Decision: Require explicit approval before adding a target trusted-proxy entry. A matching entry that already exists but is not app-managed is reported as already trusted and remains unmanaged; this version does not offer adoption. Only remove an entry proven to be app-managed. Preserve every other HTTP field and proxy entry, including IPv4. Polling cannot create/replace approval state. Serialize polling, preview, apply and recovery; bind approval to the authenticated user, DNS candidate, full normalized config hash and active config slot. Reject stale previews; recheck DNS/config immediately before configure. Persist a minimal write-ahead transaction before configure. If outcome is uncertain, inspect HA read-only and never retry. Do not overwrite a manually pending HTTP change.
- Reason: HA config may contain unrelated user-managed HTTP state and its configure API is not an atomic compare-and-swap.
- Consequences: HA has no atomic conditional configure; an external edit can still race between final config read and configure. Any observed config change requires a new review. Recovery matches exact source/candidate fingerprints and HA active slot; unknown state clears transaction attribution without claiming ownership and requires fresh review. No credentials or full HTTP config are persisted. A failed pending HA config is left untouched and blocks app writes until manually resolved. Existing matching user entries are never silently adopted.
- Provenance: operator-approved project specification supplied 2026-10-26.

## ADR-004: HA API boundary and support floor

- Status: Provisional; 2026-10-26.
- Decision: Use HA's HTTP config WebSocket API, never internal storage or YAML edits. Target HA 2026.10.0+; source-reviewed against 2026.10.4. Read `stable`/`pending`/`default`; strip server metadata before configure. Let HA own restart and five-minute rollback/confirmation. Require Home Assistant API access through Supervisor; no user token or config mount.
- Reason: HTTP settings are managed through the HA API; raw storage writes bypass validation and recovery.
- Alternatives rejected: direct `.storage` manipulation, `http` YAML editing, `http/config/promote`, and app-issued restart/confirmation.
- Consequences and review trigger: Core source contracts are checked at exact 2026.10.4 commit, but 2026.10.0 compatibility, Supervisor service paths, and behavior against a live HA remain unverified. Until live tests pass, this app is not production-ready.
- Provenance: HA Core 2026.10.4 commit `357cae5c598dfcd7a0ea7dc5b1facccd4ec6037b`; Supervisor docs cited in `DEPENDENCIES.md` without exact source revision.

## ADR-005: Ingress trust boundary

- Status: Provisional; 2026-10-26.
- Decision: Accept requests only when the actual peer is `172.30.32.2`, require `X-Remote-User-Id`, verify the user with HA `config/auth/list` active status and `system-admin` group/owner status, use unpredictable user-bound CSRF/preview approval nonces expiring after 10 minutes, and make state changes POST-only. Escape DNS/config-derived display data; keep all links relative; never expose the ephemeral Ingress token.
- Reason: Ingress is the only UI surface for an administrator-level configuration action.
- Consequences and review trigger: HA admin-list response shape is source-verified; exact headers and peer behavior vary with runtime topology and must be validated with the supported Supervisor before release. Missing/unknown identity must always deny access.
- Provenance: HA Ingress docs cited in `DEPENDENCIES.md`.

## ADR-006: Public release and bootstrap status

- Status: Accepted; 2026-10-26.
- Decision: Repository is public, multi-app, MIT licensed. Root `VERSION` is currently `0.1.0` and matches the first app version; whether future independent apps retain per-app versions while root version tracks repository releases is provisional. Images are locally device-built for amd64/aarch64. Operator-approved source work may include checkpoint commits and pushes to the feature branch under workspace standards. The separate delegated-worker instruction for this repair round prohibits that worker from committing or pushing; it is not a repository or coordinator policy. Source approval does not imply installation, deployment, image/release publication, or promotion.
- Reason: Separate source review from operational rollout.
- Consequences: No external image is referenced or published. The history review remains preliminary because prior AI histories were unavailable; no claim of complete bootstrap audit. Revisit versioning before adding a second app or publishing a release.
- Provenance: operator-approved project specification and bootstrap source inventory in `docs/BOOTSTRAP-HISTORY.md`.
