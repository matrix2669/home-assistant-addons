import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from ipv6_proxy_monitor.app.core import Coordinator
from ipv6_proxy_monitor.run import HAClient, Runtime, UnsupportedHomeAssistant, create_app


class FakeBackend:
    def __init__(self, session=None, token=None, ws_url=None):
        self.config = {"trusted_proxies": ["192.0.2.0/24"],
                       "use_x_forwarded_for": True,
                       "cors_allowed_origins": [], "server_port": 8123,
                       "pending": None, "pending_error": False,
                       "active_config_type": "stable"}
        self.users = [
            {"id": "admin", "is_active": True, "group_ids": ["system-admin"]},
            {"id": "owner", "is_active": True, "is_owner": True, "group_ids": []},
            {"id": "regular", "is_active": True, "group_ids": ["system-users"]},
            {"id": "inactive", "is_active": False, "group_ids": ["system-admin"]},
        ]
        self.calls = []
        self.fail_reads = 0

    async def http_config(self):
        if self.fail_reads:
            self.fail_reads -= 1
            raise ConnectionError("offline")
        return dict(self.config)

    async def configure(self, config):
        self.calls.append(dict(config))
        self.config["pending"] = dict(config)
        self.config["active_config_type"] = "pending"
        return True

    async def is_admin(self, user_id):
        from ipv6_proxy_monitor.app.core import user_is_admin
        return user_is_admin(self.users, user_id)


class UnsupportedBackend(FakeBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_error = "unsupported"

    async def is_admin(self, _user_id):
        raise UnsupportedHomeAssistant("old version")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_distinguishes_pending_confirmation_and_unsupported_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = root / "options.json"
            options.write_text(json.dumps({"hostname": "proxy.example", "cidr": 64,
                                           "recheck_interval": 5}))
            backend = FakeBackend()
            backend.config["pending"] = {"trusted_proxies": ["2001:db8::/64"], "error": None}
            pending_runtime = Runtime(options_path=options, state_path=root / "pending.json",
                                      token_provider=lambda: "token", peer_allowed=lambda _: True,
                                      backend_factory=lambda *a, **k: backend,
                                      resolver=lambda _: async_result(["2001:db8::1"]), poll_interval=3600)
            async with TestClient(TestServer(create_app(pending_runtime))) as client:
                response = await client.get("/review", headers={"X-Remote-User-Id": "admin"})
                content = await response.text()
                self.assertEqual(response.status, 200)
                self.assertIn("Manually confirm in Home Assistant within five minutes", content)
                self.assertEqual(backend.calls, [])

            unsupported_runtime = Runtime(options_path=options, state_path=root / "unsupported.json",
                                          token_provider=lambda: "token", peer_allowed=lambda _: True,
                                          backend_factory=lambda *a, **k: UnsupportedBackend(),
                                          poll_interval=3600)
            async with TestClient(TestServer(create_app(unsupported_runtime))) as client:
                response = await client.get("/review", headers={"X-Remote-User-Id": "admin"})
                content = await response.text()
                self.assertEqual(response.status, 503)
                self.assertIn("Unsupported Home Assistant version", content)

    async def test_json_store_atomic_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            from ipv6_proxy_monitor.run import JsonStore
            store = JsonStore(path)
            await store.write({"transaction": {"id": "tx", "target_network": "2001:db8::/64"}})
            self.assertEqual(await store.read(), {"transaction": {"id": "tx", "target_network": "2001:db8::/64"}})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_suffix(".tmp").exists())

    async def test_ingress_admin_get_review_html_escaping_and_explicit_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = root / "options.json"
            options.write_text(json.dumps({"hostname": "proxy<svg>.example", "cidr": 64,
                                           "recheck_interval": 5}))
            backend = FakeBackend()
            runtime = Runtime(
                options_path=options, state_path=root / "state.json",
                token_provider=lambda: "test-token", peer_allowed=lambda _peer: True,
                backend_factory=lambda *args, **kwargs: backend,
                resolver=lambda _name: async_result(["2001:db8::1"]), poll_interval=3600)
            async with TestClient(TestServer(create_app(runtime))) as client:
                response = await client.get("/review", headers={"X-Remote-User-Id": "admin"})
                body = await response.text()
                self.assertEqual(response.status, 200)
                self.assertIn("proxy&lt;svg&gt;.example", body)
                self.assertNotIn("<svg>", body)
                self.assertEqual(backend.calls, [])  # GET is preview-only.
                nonce = re.search(r'name=nonce value="([^"]+)"', body).group(1)

                bad = await client.post("/apply", data={"nonce": "wrong"},
                                        headers={"X-Remote-User-Id": "admin"})
                self.assertEqual(bad.status, 409)
                self.assertEqual(backend.calls, [])

                external = await runtime.store.read()
                backend.config["cors_allowed_origins"] = ["https://external.example"]
                stale = await client.post("/apply", data={"nonce": nonce},
                                          headers={"X-Remote-User-Id": "admin"})
                self.assertEqual(stale.status, 409)
                self.assertEqual(backend.calls, [])

                fresh = await client.get("/review", headers={"X-Remote-User-Id": "admin"})
                fresh_nonce = re.search(r'name=nonce value="([^"]+)"', await fresh.text()).group(1)
                applied = await client.post("/apply", data={"nonce": fresh_nonce},
                                            headers={"X-Remote-User-Id": "admin"})
                self.assertEqual(applied.status, 200)
                self.assertEqual(len(backend.calls), 1)
                self.assertEqual(backend.calls[0]["use_x_forwarded_for"], True)
                self.assertEqual(backend.calls[0]["cors_allowed_origins"], ["https://external.example"])

    async def test_missing_regular_inactive_and_non_ingress_peer_denied(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = root / "options.json"
            options.write_text(json.dumps({"hostname": "proxy.example", "cidr": 64,
                                           "recheck_interval": 5}))
            backend = FakeBackend()
            runtime = Runtime(options_path=options, state_path=root / "state.json",
                              token_provider=lambda: "token", peer_allowed=lambda peer: peer == "127.0.0.1",
                              backend_factory=lambda *a, **k: backend,
                              resolver=lambda _: async_result(["2001:db8::1"]), poll_interval=3600)
            async with TestClient(TestServer(create_app(runtime))) as client:
                for headers in ({}, {"X-Remote-User-Id": "regular"},
                                {"X-Remote-User-Id": "inactive"}):
                    response = await client.get("/review", headers=headers)
                    self.assertEqual(response.status, 403)
                # A forged forwarding header cannot override the actual-peer check.
                runtime.peer_allowed = lambda _peer: False
                response = await client.get("/review", headers={
                    "X-Remote-User-Id": "admin", "X-Forwarded-For": "172.30.32.2"})
                self.assertEqual(response.status, 403)
                runtime.peer_allowed = lambda peer: peer == "127.0.0.1"
                response = await client.get("/review", headers={"X-Remote-User-Id": "owner"})
                self.assertEqual(response.status, 200)

    async def test_start_survives_ha_outage_then_poll_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = root / "options.json"
            options.write_text(json.dumps({"hostname": "", "cidr": 128,
                                           "recheck_interval": 5}))
            backend = FakeBackend()
            runtime = Runtime(options_path=options, state_path=root / "state.json",
                              token_provider=lambda: "token",
                              backend_factory=lambda *a, **k: backend,
                              resolver=lambda _: async_result(["2001:db8::1"]), poll_interval=3600)
            async with TestClient(TestServer(create_app(runtime))) as client:
                self.assertIsNotNone(runtime.coordinator)
                runtime.options["hostname"] = "proxy.example"
                backend.fail_reads = 1
                self.assertIsNone(await runtime.poll_once())
                result = await runtime.poll_once()
                self.assertEqual(result.status, "network_changed")
                self.assertEqual(backend.calls, [])

    async def test_notification_create_deduplicate_dismiss_and_transient_dns_preserves(self):
        received = []
        attempts = 0
        async def addon_info(_request):
            return web.json_response({"data": {"slug": "repo_hash_ipv6_proxy_monitor"}})
        async def create(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return web.Response(status=503)
            received.append(("create", await request.json()))
            return web.json_response([])
        async def dismiss(request):
            received.append(("dismiss", await request.json()))
            return web.json_response([])
        fake_app = web.Application()
        fake_app.router.add_get("/addons/self/info", addon_info)
        fake_app.router.add_post("/core/api/services/persistent_notification/create", create)
        fake_app.router.add_post("/core/api/services/persistent_notification/dismiss", dismiss)
        async with TestServer(fake_app) as server:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                session = ClientSession(timeout=ClientTimeout(total=2))
                runtime = Runtime(
                    session_factory=lambda **kwargs: session,
                    options_path=root / "options.json", state_path=root / "state.json",
                    supervisor_url=str(server.make_url("")).rstrip("/"),
                    token_provider=lambda: "never-logged-test-token", poll_interval=3600)
                runtime.session = session
                self.assertFalse(await runtime.set_notification("changed", "Changed", "Review now"))
                self.assertTrue(await runtime.set_notification("changed", "Changed", "Review now"))
                self.assertTrue(await runtime.set_notification("changed", "Changed", "Review now"))
                self.assertEqual(len(received), 1)
                self.assertEqual(attempts, 2)  # first service failure was retried next poll
                body = received[0][1]
                self.assertIn("/hassio/ingress/repo_hash_ipv6_proxy_monitor", body["message"])
                self.assertNotIn("never-logged-test-token", body["message"])
                runtime.options = {"hostname": "proxy.example", "cidr": 64}
                runtime.store = runtime.store_factory(root / "state.json")
                runtime.ha = FakeBackend()
                runtime.coordinator = Coordinator(runtime.ha, runtime.store,
                                                  failing_resolver)
                before = runtime.notification_state
                await runtime.poll_once()
                self.assertEqual(runtime.notification_state, before)
                self.assertTrue(await runtime.dismiss_notification())
                self.assertEqual(received[-1][0], "dismiss")
                await session.close()

    async def test_ha_recovery_read_failure_with_wal_creates_warning_never_dismisses(self):
        calls = []
        async def addon_info(_request):
            return web.json_response({"data": {"slug": "repo_hash_ipv6_proxy_monitor"}})
        async def create(request):
            calls.append(("create", await request.json()))
            return web.json_response([])
        async def dismiss(request):
            calls.append(("dismiss", await request.json()))
            return web.json_response([])
        supervisor_app = web.Application()
        supervisor_app.router.add_get("/addons/self/info", addon_info)
        supervisor_app.router.add_post("/core/api/services/persistent_notification/create", create)
        supervisor_app.router.add_post("/core/api/services/persistent_notification/dismiss", dismiss)
        async with TestServer(supervisor_app) as supervisor:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                options = root / "options.json"
                options.write_text(json.dumps({"hostname": "", "cidr": 64,
                                               "recheck_interval": 5}))
                backend = FakeBackend()
                runtime = Runtime(
                    options_path=options, state_path=root / "state.json",
                    supervisor_url=str(supervisor.make_url("")).rstrip("/"),
                    token_provider=lambda: "runtime-test-token", peer_allowed=lambda _: True,
                    backend_factory=lambda *a, **k: backend,
                    resolver=lambda _: async_result(["2001:db8::1"]), poll_interval=3600)
                async with TestClient(TestServer(create_app(runtime))):
                    runtime.options["hostname"] = "proxy.example"
                    await runtime.store.write({
                        "managed_network": None,
                        "managed_entry": None,
                        "approval": None,
                        "transaction": {
                            "id": "uncertain-test", "phase": "configure_uncertain",
                            "source_fingerprint": "source", "source_active_type": "stable",
                            "candidate_fingerprint": "candidate", "candidate_active_type": "pending",
                            "target_network": "2001:db8::/64", "target_entry": "2001:db8::/64",
                            "previous_managed_network": None, "previous_managed_entry": None,
                            "observed_trial": False,
                        },
                    })
                    backend.fail_reads = 1
                    result = await runtime.poll_once()
                    self.assertEqual(result.status, "configure_outcome_requires_manual_inspection")
                    saved = await runtime.store.read()
                    self.assertIsNotNone(saved["transaction"])
                    self.assertEqual(runtime.notification_state, "uncertain")
                    self.assertEqual([kind for kind, _ in calls], ["create"])
                    self.assertIn("will not retry or claim success", calls[0][1]["message"])

    async def test_ha_client_version_gate_timeout_reconnect_and_no_command_retry(self):
        connections = 0
        configure_calls = 0
        async def ws_handler(request):
            nonlocal connections, configure_calls
            connections += 1
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "auth_required"})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = msg.json()
                if data.get("type") == "auth":
                    await ws.send_json({"type": "auth_ok"})
                elif data.get("type") == "get_config":
                    await ws.send_json({"id": data["id"], "type": "result", "success": True,
                                        "result": {"version": "2026.10.4"}})
                elif data.get("type") == "http/config":
                    await ws.send_json({"id": data["id"], "type": "result", "success": True,
                                        "result": {"stable": {"trusted_proxies": []},
                                                   "pending": None, "active_config_type": "stable"}})
                elif data.get("type") == "http/config/configure":
                    configure_calls += 1  # deliberately drop reply to simulate uncertain response
            return ws
        fake_app = web.Application()
        fake_app.router.add_get("/api/websocket", ws_handler)
        async with TestServer(fake_app) as server:
            async with ClientSession() as session:
                client = HAClient(session, "test-token", ws_url=str(server.make_url("/api/websocket")),
                                  timeout=0.12)
                config = await client.http_config()
                self.assertEqual(config["active_config_type"], "stable")
                with self.assertRaises(asyncio.TimeoutError):
                    await client.configure({"trusted_proxies": ["2001:db8::/64"]})
                self.assertEqual(configure_calls, 1)
                self.assertIsNone(client.ws)
                # A later read reconnects; the timed-out configure is not replayed.
                await client.http_config()
                self.assertEqual(configure_calls, 1)
                self.assertGreaterEqual(connections, 2)
                await client._reset()

        async def old_ws(request):
            ws = web.WebSocketResponse(); await ws.prepare(request)
            await ws.send_json({"type": "auth_required"})
            async for msg in ws:
                data = msg.json()
                if data.get("type") == "auth": await ws.send_json({"type": "auth_ok"})
                elif data.get("type") == "get_config":
                    await ws.send_json({"id": data["id"], "success": True,
                                        "result": {"version": "2026.9.9"}})
            return ws
        old_app = web.Application(); old_app.router.add_get("/api/websocket", old_ws)
        async with TestServer(old_app) as server:
            async with ClientSession() as session:
                client = HAClient(session, "token", ws_url=str(server.make_url("/api/websocket")), timeout=1)
                with self.assertRaises(Exception):
                    await client.http_config()
                self.assertEqual(client.last_error, "unsupported")


async def async_result(value):
    return list(value)


async def failing_resolver(_hostname):
    raise TimeoutError("synthetic DNS timeout")


if __name__ == "__main__":
    unittest.main()
