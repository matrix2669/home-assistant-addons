# IPv6 Proxy Monitor

Standalone Home Assistant app. It periodically resolves AAAA records, computes IPv6 networks, and creates a persistent notification when an unambiguous candidate differs from the active trusted-proxy state. The notification's **Open review** link opens the app at `/app/<slug>`. Only an authenticated HA administrator's explicit fresh Ingress approval may send `http/config/configure`.

Settings are `hostname`, `cidr` (1–128, default 128), and `recheck_interval` in minutes (1–1440, default 5). A pre-existing matching entry remains unmanaged; this version never silently adopts it. Network errors, invalid/empty DNS answers, unsupported HA versions, pending manual configuration, and ambiguous candidates fail closed. See `DOCS.md` and repository `DECISIONS.md` / `DEPENDENCIES.md` for the manual restart/confirmation flow and known API/concurrency limitations.

Builds use Home Assistant's device build mechanism from `Dockerfile`; the app exposes only internal Ingress port 8099 and mounts no host/config paths. Source candidate only; no image or release has been published.
