# Home Assistant apps by matrix2669

This public repository contains standalone Home Assistant apps. **IPv6 Proxy Monitor** watches the IPv6 network behind one configured hostname and asks before changing Home Assistant's HTTP trusted-proxy settings.

## IPv6 Proxy Monitor

The first app is an early source candidate, not a released or production-validated app. It requires Home Assistant 2026.10.0 or newer (target support; exact compatibility remains to be verified). Install it through the Home Assistant Apps store only after a reviewed release is published; this repository task does not install or deploy it.

Configuration has three settings:

| Setting | Meaning | Default |
|---|---|---|
| `hostname` | DNS hostname whose AAAA addresses identify the proxy network | required |
| `cidr` | IPv6 prefix length (integer 1–128) used to calculate the network | `128` |
| `recheck_interval` | DNS check cadence in minutes (integer 1–1440) | `5` |

The app uses bitwise IPv6 network calculation, not string matching. AAAA results mapping to more than one distinct network block approval and produce a warning. DNS errors, empty answers and invalid results never change Home Assistant configuration. The app does not restart HA automatically or confirm pending HTTP settings.

When a changed network is detected, a persistent notification links to the app's relative internal Ingress review page. Review the hostname, prefix, current and proposed network. Select **Apply and restart** only if the proposal is correct. This is the only action that may submit a change to Home Assistant's HTTP configuration API. Home Assistant may restart in response. After it comes back, manually review and confirm the HTTP settings in Home Assistant within five minutes; otherwise Home Assistant rolls them back and restarts. The app does not confirm on your behalf.

Adding a new target entry requires explicit approval. A matching trusted-proxy entry that already exists but was not created by this app remains unmanaged; this version will not adopt or later remove it. The app only removes a trusted-proxy entry it previously recorded as explicitly managed and preserves other settings, including IPv4. Existing manually pending HTTP changes block apply. HA does not offer an atomic conditional save, so an external edit can still race the app's final recheck; see `DEPENDENCIES.md` and `DECISIONS.md`.

## Development

See `ipv6_proxy_monitor/README.md` for app source/build details and `AGENT.md` for developer safety rules. Offline tests: `python3 -m unittest discover -s tests -v`. No image, release or deployment is published by this repository source candidate.
