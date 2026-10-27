# IPv6 Proxy Monitor

IPv6 Proxy Monitor watches AAAA answers for one hostname and requests approval before adding a changed IPv6 network to Home Assistant HTTP trusted proxies.

## Options

| Option | Description | Default |
|---|---|---|
| `hostname` | Hostname to query for AAAA records | empty (monitor paused) |
| `cidr` | Prefix length used for bitwise network calculation | `128` |
| `recheck_interval` | Poll period in minutes | `5` |

One unambiguous candidate network is required. Multiple distinct networks, empty answers, invalid addresses, and DNS failures never change HA settings. A pre-existing matching trusted-proxy entry remains external/unmanaged; this version will not adopt or later remove it.

## Approval and recovery

A persistent notification links to the app's Home Assistant frontend page at `/app/<slug>`. Review the hostname, prefix, current managed network, proposed network, current proxy list, `use_x_forwarded_for` status, and the full-configuration fingerprint. The app does not silently enable forwarded-header trust or modify unrelated HTTP settings. Choose **Apply and restart** to submit one explicitly approved candidate. HA controls its restart and pending trial. Manually confirm the HTTP settings in HA within five minutes; the app never confirms them. If HA does not confirm, it rolls back and restarts.

The app rechecks DNS and compares the exact normalized stable HTTP config snapshot immediately before writing. Any external HTTP config change invalidates the preview. If `use_x_forwarded_for` is disabled, the app blocks and requires an administrator to decide whether to enable it directly in HA; the app never changes that setting. A write-ahead journal stores hashes, transaction state, and target/previous managed network identities, not HTTP credential/config payloads. Ambiguous configure outcomes are inspected read-only and never retried. A stable source configuration immediately after a timeout is not evidence of rejection: HA may accept a delayed configure request later. Until the matching pending trial is observed, the app retains the journal and blocks further writes. This experimental version has no manual journal-clear action, so an unresolved outcome can remain locked indefinitely; inspect HA HTTP settings and do not retry or delete app state. HA failed-pending settings remain untouched and block a new app write until an administrator resolves them in HA.

The app requires HA 2026.10.0+, is marked **Experimental**, has no exposed host port or config mount, and is not yet live-validated. Its image is built locally by Home Assistant; source has not been installed or deployed by this project task.
