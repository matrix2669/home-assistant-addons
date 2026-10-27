"""Home Assistant app runtime and authenticated Ingress review UI."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiohttp import web
import dns.asyncresolver

from .app.core import (Coordinator, PolicyError, Preview, PollResult,
                       configure_payload, ingress_peer_allowed,
                       user_is_admin)

LOG = logging.getLogger("ipv6_proxy_monitor")
RUNTIME_KEY = web.AppKey("runtime", object)
PORT = 8099
SUPERVISOR = "http://supervisor"
WS_URL = "ws://supervisor/core/websocket"
STATE_PATH = Path("/data/state.json")
OPTIONS_PATH = Path("/data/options.json")
MIN_HA_VERSION = (2026, 10, 0)
IO_TIMEOUT = 10
NOTIFICATION_ID = "ipv6_proxy_monitor_network_change"


class JsonStore:
    def __init__(self, path: Path):
        self.path = path

    async def read(self) -> dict:
        try:
            return json.loads(await asyncio.to_thread(self.path.read_text, encoding="utf-8"))
        except FileNotFoundError:
            return {}

    async def write(self, value: dict) -> None:
        await asyncio.to_thread(self._atomic_write, value)

    def _atomic_write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


class UnsupportedHomeAssistant(RuntimeError):
    """The connected Home Assistant is below the declared minimum version."""


class HAClient:
    """Bounded single-flight WebSocket client; failed sessions are never reused."""

    def __init__(self, session: aiohttp.ClientSession, token: str,
                 ws_url: str = WS_URL, timeout: float = IO_TIMEOUT):
        self.session, self.token, self.ws_url, self.timeout = session, token, ws_url, timeout
        self.ws = None
        self.next_id = 1
        self.command_lock = asyncio.Lock()
        self.version: str | None = None
        self.last_error = "unavailable"

    async def _connect(self) -> None:
        ws_timeout = aiohttp.ClientWSTimeout(ws_receive=self.timeout, ws_close=self.timeout)
        self.ws = await self.session.ws_connect(self.ws_url, timeout=ws_timeout)
        greeting = await self.ws.receive_json(timeout=self.timeout)
        if greeting.get("type") != "auth_required":
            raise RuntimeError("Unexpected Home Assistant WebSocket greeting")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        response = await self.ws.receive_json(timeout=self.timeout)
        if response.get("type") != "auth_ok":
            raise RuntimeError("Home Assistant authentication failed")
        config = await self._raw_command("get_config")
        version = str((config or {}).get("version", ""))
        match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < MIN_HA_VERSION:
            self.version = version or None
            self.last_error = "unsupported"
            raise UnsupportedHomeAssistant("Home Assistant 2026.10.0 or newer is required")
        self.version = version
        self.last_error = ""

    async def _raw_command(self, command_type: str, **params):
        request_id = self.next_id
        self.next_id += 1
        await self.ws.send_json({"id": request_id, "type": command_type, **params})
        while True:
            response = await self.ws.receive_json(timeout=self.timeout)
            if response.get("id") != request_id:
                continue
            if not response.get("success"):
                raise RuntimeError("Home Assistant rejected a request")
            return response.get("result")

    async def _reset(self) -> None:
        ws, self.ws = self.ws, None
        self.version = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
            try:
                ws._response.close()
                ws._response.release()
            except Exception:
                pass

    async def close(self) -> None:
        async with self.command_lock:
            await self._reset()

    async def command(self, command_type: str, **params):
        async with self.command_lock:
            try:
                async with asyncio.timeout(self.timeout):
                    if self.ws is None or self.ws.closed or self.version is None:
                        await self._connect()
                    return await self._raw_command(command_type, **params)
            except UnsupportedHomeAssistant:
                await self._reset()
                self.last_error = "unsupported"
                raise
            except Exception:
                await self._reset()
                self.last_error = "unavailable"
                raise

    async def http_config(self) -> dict:
        result = await self.command("http/config")
        stable = result.get("stable") or result.get("default") or {}
        pending = result.get("pending")
        return {**stable, "pending": pending,
                "pending_error": bool(isinstance(pending, dict) and pending.get("error")),
                "active_config_type": result.get("active_config_type"),
                "revert_at": result.get("revert_at")}

    async def configure(self, config: dict) -> bool:
        result = await self.command("http/config/configure", config=configure_payload(config))
        return bool(result.get("restart"))

    async def is_admin(self, user_id: str) -> bool:
        users = await self.command("config/auth/list")
        return isinstance(users, list) and user_is_admin(users, user_id)


async def resolve_aaaa(hostname: str) -> list[str]:
    answer = await dns.asyncresolver.resolve(hostname, "AAAA", lifetime=IO_TIMEOUT)
    return [str(item) for item in answer]


class Runtime:
    """Dependencies and paths are injectable for offline runtime/HTTP tests."""

    def __init__(self, *, session_factory=aiohttp.ClientSession, backend_factory=HAClient,
                 resolver=resolve_aaaa, store_factory=JsonStore,
                 options_path: Path = OPTIONS_PATH, state_path: Path = STATE_PATH,
                 supervisor_url: str = SUPERVISOR, ws_url: str = WS_URL,
                 token_provider=lambda: os.environ.get("SUPERVISOR_TOKEN"),
                 poll_interval: int | None = None, peer_allowed=ingress_peer_allowed):
        self.session_factory = session_factory
        self.backend_factory = backend_factory
        self.resolver = resolver
        self.store_factory = store_factory
        self.options_path = options_path
        self.state_path = state_path
        self.supervisor_url = supervisor_url
        self.ws_url = ws_url
        self.token_provider = token_provider
        self.poll_interval = poll_interval
        self.peer_allowed = peer_allowed
        self.session = None
        self.ha = None
        self.coordinator = None
        self.store = None
        self.options: dict = {"hostname": "", "cidr": 128, "recheck_interval": 5}
        self.slug = None
        self.notification_state: str | None = None
        self.notification_lock = asyncio.Lock()
        self.poll_task: asyncio.Task | None = None
        self.closed = False

    async def start(self, app: web.Application) -> None:
        self.closed = False
        try:
            self.options = json.loads(await asyncio.to_thread(self.options_path.read_text, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOG.warning("App options unavailable; monitoring is paused")
        self.store = self.store_factory(self.state_path)
        timeout = aiohttp.ClientTimeout(total=IO_TIMEOUT)
        self.session = self.session_factory(timeout=timeout)
        token = self.token_provider()
        if token:
            self.ha = self.backend_factory(self.session, token, ws_url=self.ws_url)
            self.coordinator = Coordinator(self.ha, self.store, self.resolver)
        app[RUNTIME_KEY] = self
        self.poll_task = asyncio.create_task(self.poll_loop())

    async def stop(self, app: web.Application) -> None:
        self.closed = True
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
            self.poll_task = None
        if self.ha and hasattr(self.ha, "close"):
            await self.ha.close()
        if self.session:
            await self.session.close()
            self.session = None

    async def refresh_slug(self) -> str | None:
        if self.slug:
            return self.slug
        token = self.token_provider()
        if not token or not self.session:
            return None
        try:
            async with self.session.get(f"{self.supervisor_url}/addons/self/info",
                                        headers={"Authorization": f"Bearer {token}"}) as response:
                response.raise_for_status()
                self.slug = (await response.json())["data"]["slug"]
        except Exception:
            return None
        return self.slug

    async def poll_loop(self) -> None:
        while not self.closed:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Keep service/UI available; never log backend payloads or credentials.
                LOG.warning("Monitor check unavailable; no HTTP configuration was changed")
            delay = self.poll_interval or max(1, int(self.options.get("recheck_interval", 5))) * 60
            await asyncio.sleep(delay)

    async def poll_once(self) -> PollResult | None:
        if not self.coordinator or not self.options.get("hostname"):
            return None
        try:
            result = await self.coordinator.poll(
                self.options["hostname"], int(self.options.get("cidr", 128)))
        except PolicyError as err:
            if str(err) == "Multiple IPv6 networks returned":
                await self.set_notification("ambiguous", "IPv6 proxy networks are ambiguous",
                                            "AAAA answers map to multiple networks. Review DNS.")
            elif str(err) in {"No AAAA addresses returned", "Invalid AAAA response"}:
                await self.set_notification("dns", "IPv6 proxy DNS check failed",
                                            "DNS did not return a usable AAAA answer. No HA change was made.")
            # Other temporary failures leave the existing notification untouched.
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            LOG.warning("DNS or Home Assistant check unavailable; existing notification retained")
            return None
        transaction_active = bool((await self.store.read()).get("transaction"))
        if result.status == "network_changed":
            await self.set_notification("changed", "IPv6 proxy network changed",
                                        "Review the hostname, prefix, current and proposed networks in the app.")
        elif result.status in {"awaiting_user_confirmation", "awaiting_home_assistant_trial"}:
            await self.set_notification("pending", "HTTP settings await review",
                                        "Review and manually confirm in Home Assistant within five minutes; this app will not confirm them.")
        elif result.status in {"pending_configuration", "external_pending_configuration"}:
            await self.set_notification("manual_pending", "HTTP settings already pending",
                                        "Resolve the existing Home Assistant HTTP change before using this app.")
        elif result.status == "pending_configuration_failed":
            await self.set_notification("failed", "Home Assistant rejected pending HTTP settings",
                                        "Inspect the failed pending settings in Home Assistant. The app will not retry or confirm them.")
        elif result.status == "configure_outcome_requires_manual_inspection":
            await self.set_notification(
                "uncertain", "HTTP change outcome remains uncertain",
                "Inspect Home Assistant HTTP settings and any pending trial. The app has retained its transaction lock and will not retry or claim success; this state may remain locked until a future safe recovery path is available.")
        elif result.status == "external_http_configuration_change":
            await self.set_notification("external", "HTTP settings changed outside this app",
                                        "Review Home Assistant HTTP settings before starting another app change.")
        elif result.status == "rolled_back_after_observed_trial":
            await self.set_notification("rolled_back", "Home Assistant rolled back the HTTP change",
                                        "The previously observed trial is no longer active. Review the trusted-proxy settings in Home Assistant; the app did not retry.")
        elif result.status == "confirmed_by_home_assistant":
            await self.dismiss_notification()
        elif result.status == "managed_entry_missing":
            await self.set_notification("managed_missing", "Managed trusted proxy is missing",
                                        "The previously managed entry was removed externally; review the app and HA settings.")
        else:
            if transaction_active:
                await self.set_notification(
                    "unresolved_transaction", "HTTP transaction needs inspection",
                    "An app transaction remains unresolved. Inspect Home Assistant HTTP settings; the app will not retry or dismiss this warning automatically.")
            elif result.status in {"unchanged", "already_trusted_unmanaged"}:
                await self.dismiss_notification()
            else:
                await self.set_notification(
                    "unrecognized", "Monitor status needs inspection",
                    "The monitor returned an unrecognized status. Review Home Assistant HTTP settings before taking action.")
        return result

    async def set_notification(self, state: str, title: str, message: str) -> bool:
        async with self.notification_lock:
            if self.notification_state == state:
                return True
            slug = await self.refresh_slug()
            token = self.token_provider()
            if not slug or not token or not self.session:
                return False
            ingress_url = f"/app/{quote(slug, safe='')}"
            body = {"notification_id": NOTIFICATION_ID, "title": title,
                    "message": f"{message}\n\n[Open review]({ingress_url})"}
            try:
                async with self.session.post(
                    f"{self.supervisor_url}/core/api/services/persistent_notification/create",
                    headers={"Authorization": f"Bearer {token}"}, json=body) as response:
                    response.raise_for_status()
            except Exception:
                return False
            self.notification_state = state
            return True

    async def dismiss_notification(self) -> bool:
        async with self.notification_lock:
            if self.notification_state is None:
                # Dismiss once after startup too: the notification survives app restarts.
                self.notification_state = "unknown"
            token = self.token_provider()
            if not token or not self.session:
                return False
            try:
                async with self.session.post(
                    f"{self.supervisor_url}/core/api/services/persistent_notification/dismiss",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"notification_id": NOTIFICATION_ID}) as response:
                    response.raise_for_status()
            except Exception:
                return False
            self.notification_state = None
            return True

    async def authorized(self, request: web.Request) -> str | None:
        peer = request.transport.get_extra_info("peername") if request.transport else None
        if not peer or not self.peer_allowed(peer[0]) or not self.ha:
            return None
        user_id = request.headers.get("X-Remote-User-Id")
        if not user_id:
            return None
        try:
            return user_id if await self.ha.is_admin(user_id) else None
        except UnsupportedHomeAssistant:
            return None
        except Exception:
            return None


def create_app(runtime: Runtime | None = None) -> web.Application:
    rt = runtime or Runtime()
    app = web.Application(client_max_size=4096)
    app[RUNTIME_KEY] = rt
    app.on_startup.append(rt.start)
    app.on_cleanup.append(rt.stop)
    app.router.add_get("/", review)
    app.router.add_get("/review", review)
    app.router.add_post("/apply", apply)
    app.router.add_get("/health", health)
    return app


def page(content: str, status: int = 200) -> web.Response:
    return web.Response(text="<!doctype html><meta charset=utf-8><title>IPv6 Proxy Monitor</title>"
                        f"<main>{content}</main>", content_type="text/html", status=status)


async def review(request: web.Request) -> web.Response:
    rt: Runtime = request.app[RUNTIME_KEY]
    user_id = await rt.authorized(request)
    if not user_id:
        if rt.ha and getattr(rt.ha, "last_error", "") == "unsupported":
            return page("<h1>Unsupported Home Assistant version</h1><p>Home Assistant 2026.10.0 or newer is required.</p>", 503)
        if rt.ha and getattr(rt.ha, "last_error", "") == "unavailable":
            return page("<h1>Home Assistant unavailable</h1><p>Administrator verification could not complete. Try again when Home Assistant is available.</p>", 503)
        return page("<h1>Administrator authentication required</h1>", 403)
    if not rt.coordinator:
        return page("<h1>Monitor unavailable</h1><p>Home Assistant API is not ready; try again later.</p>", 503)
    hostname = str(rt.options.get("hostname", ""))
    prefix = int(rt.options.get("cidr", 128))
    try:
        preview = await rt.coordinator.preview(hostname, prefix, user_id)
    except PolicyError as err:
        if "use_x_forwarded_for is disabled" in str(err):
            return page("<h1>Forwarded-header trust is disabled</h1><p>This app will not enable it. If this proxy requires forwarded headers, an administrator must enable `use_x_forwarded_for` explicitly in Home Assistant and then review again.</p>")
        if "outcome remains uncertain" in str(err):
            return page("<h1>HTTP change outcome remains uncertain</h1><p>Inspect Home Assistant's current HTTP settings and any pending trial. The app retains its transaction lock because a stable old configuration does not prove a delayed configure request was rejected. Do not retry or clear app state; this condition may remain locked until a future safe recovery path is available.</p>")
        if "failed pending" in str(err):
            return page("<h1>Failed pending HTTP configuration</h1><p>Inspect and resolve the failed pending settings in Home Assistant. The app will not retry, clear, or confirm them.</p>")
        if "attention" in str(err):
            return page("<h1>HTTP settings need attention</h1><p>Review the current Home Assistant HTTP settings and transaction status before proceeding.</p>")
        try:
            current = await rt.ha.http_config()
            if current.get("pending_error"):
                return page("<h1>Home Assistant rejected pending HTTP settings</h1><p>Inspect them in Home Assistant. The app will not retry or confirm.</p>")
            if current.get("pending"):
                return page("<h1>HTTP settings awaiting confirmation</h1><p>Manually confirm in Home Assistant within five minutes or it rolls back and restarts. This app will not confirm.</p>")
        except Exception:
            pass
        if str(err) in {"No AAAA addresses returned", "Invalid AAAA response", "Multiple IPv6 networks returned"}:
            return page("<h1>DNS review unavailable</h1><p>DNS did not produce one valid IPv6 network. HA configuration was not changed.</p>")
        return page("<h1>Review unavailable</h1><p>Home Assistant settings changed or are not safe to apply. No configuration change was made; refresh for a new review.</p>")
    except Exception:
        if getattr(rt.ha, "last_error", "") == "unsupported":
            return page("<h1>Unsupported Home Assistant version</h1><p>Home Assistant 2026.10.0 or newer is required.</p>", 503)
        return page("<h1>Home Assistant unavailable</h1><p>Settings could not be read. No configuration change was made; try again when Home Assistant is available.</p>", 503)
    host = html.escape(preview.hostname)
    proxies = "<ul>" + "".join(f"<li>{html.escape(proxy)}</li>" for proxy in preview.trusted_proxies) + "</ul>"
    forwarding = "enabled" if preview.use_x_forwarded_for else "disabled"
    if preview.already_trusted:
        body = (f"<h1>Already trusted outside this app</h1><p>{host} /{preview.prefix} proposes "
                f"{html.escape(preview.candidate)}, which is already present. The app will not adopt or remove it.</p>")
    elif preview.current is not None and preview.candidate == preview.current:
        body = f"<h1>No network change</h1><p>{host} still maps to {html.escape(preview.candidate)}.</p>"
    else:
        body = (f"<h1>Review trusted proxy change</h1><p>Hostname: {host}</p>"
                f"<p>IPv6 prefix: /{preview.prefix}</p>"
                f"<p>Current app-managed network: {html.escape(preview.current or 'none')}</p>"
                f"<p>Proposed network: {html.escape(preview.candidate)}</p>"
                f"<p>use_x_forwarded_for is {forwarding}; it will not be changed.</p>"
                f"<p>Current trusted proxies (full HTTP settings fingerprint: {preview.source_fingerprint[:12]}):</p>{proxies}"
                "<p>Apply creates pending HTTP settings and may restart Home Assistant. You must manually confirm them in Home Assistant within five minutes or HA rolls them back and restarts.</p>"
                f"<form method=post action=\"./apply\"><input type=hidden name=nonce value=\"{html.escape(preview.nonce, quote=True)}\">"
                "<button type=submit>Apply and restart</button></form>")
    return page(body)


async def apply(request: web.Request) -> web.Response:
    rt: Runtime = request.app[RUNTIME_KEY]
    user_id = await rt.authorized(request)
    if not user_id:
        error = getattr(rt.ha, "last_error", "") if rt.ha else "unavailable"
        if error == "unsupported":
            return page("<h1>Unsupported Home Assistant version</h1><p>Home Assistant 2026.10.0 or newer is required.</p>", 503)
        if error == "unavailable":
            return page("<h1>Home Assistant unavailable</h1><p>Administrator verification could not complete. Try again later.</p>", 503)
        return page("<h1>Administrator authentication required</h1>", 403)
    if not rt.coordinator:
        return page("<h1>Monitor unavailable</h1>", 503)
    try:
        data = await request.post()
        state = await rt.store.read()
        approval = state.get("approval") or {}
        if (approval.get("hostname") != rt.options.get("hostname")
                or approval.get("prefix") != int(rt.options.get("cidr", 128))):
            raise PolicyError("Settings changed")
        preview = Preview(
            approval["hostname"], approval["prefix"], approval["current"],
            approval["candidate"], str(data.get("nonce", "")), user_id,
            approval["source_fingerprint"], approval["source_active_type"],
            approval["use_x_forwarded_for"], tuple(approval["trusted_proxies"]),
            approval["already_trusted"],
        )
        outcome = await rt.coordinator.apply(preview, preview.nonce, user_id)
    except PolicyError as err:
        if "outcome uncertain" in str(err):
            return page("<h1>Configure outcome uncertain</h1><p>Do not retry. Inspect Home Assistant HTTP settings and any pending confirmation first.</p>", 503)
        return page("<h1>Not applied</h1><p>Approval expired or settings changed. Review again; the app did not confirm anything.</p>", 409)
    except Exception:
        return page("<h1>Home Assistant unavailable</h1><p>The request could not be verified. Inspect HA settings before retrying.</p>", 503)
    return page(f"<h1>Request submitted</h1><p>{html.escape(outcome)}. Manually confirm in Home Assistant within five minutes.</p>")


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


def main() -> None:
    web.run_app(create_app(), host="0.0.0.0", port=PORT, access_log=None)


if __name__ == "__main__":
    main()
