import asyncio
import unittest

from ipv6_proxy_monitor.app.core import (
    Coordinator,
    PolicyError,
    config_fingerprint,
    configure_payload,
    ingress_peer_allowed,
    single_candidate,
    user_is_admin,
)


class MemoryStore:
    def __init__(self):
        self.value = {}

    async def read(self):
        return dict(self.value)

    async def write(self, value):
        self.value = dict(value)


class FakeBackend:
    def __init__(self, config=None):
        self.stable = config or {
            "trusted_proxies": ["192.0.2.0/24"],
            "use_x_forwarded_for": True,
            "cors_allowed_origins": ["https://home.example"],
            "server_port": 8123,
            "created_at": "metadata",
            "error": None,
        }
        self.pending = None
        self.active_type = "stable"
        self.calls = []
        self.accept_then_disconnect = False

    async def http_config(self):
        return {**self.stable, "pending": self.pending,
                "pending_error": bool(self.pending and self.pending.get("error")),
                "active_config_type": self.active_type, "revert_at": None}

    async def configure(self, config):
        self.calls.append(dict(config))
        self.pending = {**config, "created_at": "metadata", "error": None}
        self.active_type = "pending"
        if self.accept_then_disconnect:
            self.accept_then_disconnect = False
            raise ConnectionError("simulated disconnect after HA accepted")
        return True

    def confirm(self):
        self.stable = {k: v for k, v in self.pending.items()
                       if k not in {"created_at", "error", "error_message"}}
        self.pending = None
        self.active_type = "stable"

    def rollback(self):
        self.pending["error"] = "not_promoted"
        self.pending["error_message"] = "private server text"
        self.active_type = "stable"


class CoreTests(unittest.TestCase):
    def test_network_math_host_bits_and_multi_network_fail_closed(self):
        self.assertEqual(single_candidate(["2001:db8:abcd:1::1", "2001:db8:abcd:2::2"], 48),
                         "2001:db8:abcd::/48")
        self.assertEqual(single_candidate(["2001:db8::1"], 128), "2001:db8::1/128")
        with self.assertRaises(PolicyError):
            single_candidate(["2001:db8::1", "2001:db9::1"], 64)
        with self.assertRaises(PolicyError):
            single_candidate([], 64)
        with self.assertRaises(PolicyError):
            single_candidate(["192.0.2.1"], 64)

    def test_ingress_peer_and_active_admin_check(self):
        self.assertTrue(ingress_peer_allowed("172.30.32.2"))
        for peer in (None, "172.30.32.3", "127.0.0.1", "2001:db8::1"):
            self.assertFalse(ingress_peer_allowed(peer))
        users = [
            {"id": "admin", "is_active": True, "group_ids": ["system-admin"]},
            {"id": "owner", "is_active": True, "is_owner": True, "group_ids": []},
            {"id": "inactive", "is_active": False, "group_ids": ["system-admin"]},
            {"id": "regular", "is_active": True, "group_ids": ["system-users"]},
        ]
        self.assertTrue(user_is_admin(users, "admin"))
        self.assertTrue(user_is_admin(users, "owner"))
        self.assertFalse(user_is_admin(users, "inactive"))
        self.assertFalse(user_is_admin(users, "regular"))
        self.assertFalse(user_is_admin(users, "unknown"))

    def test_fingerprint_ignores_only_ha_metadata(self):
        config = {"trusted_proxies": ["192.0.2.0/24"], "server_port": 8123,
                  "created_at": "a", "error": None, "error_message": None}
        other_metadata = {**config, "created_at": "b"}
        changed_setting = {**config, "server_port": 8124}
        self.assertEqual(config_fingerprint(config), config_fingerprint(other_metadata))
        self.assertNotEqual(config_fingerprint(config), config_fingerprint(changed_setting))
        self.assertEqual(configure_payload(config), {"trusted_proxies": ["192.0.2.0/24"],
                                                       "server_port": 8123})

    def test_poll_never_overwrites_preview_or_transaction(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            address = ["2001:db8:1::1"]
            c = Coordinator(backend, store, lambda _: async_value(address))
            review = await c.preview("proxy.example", 64, "admin")
            original = await store.read()
            result = await c.poll("proxy.example", 64)
            self.assertEqual(result.status, "network_changed")
            self.assertEqual((await store.read())["approval"], original["approval"])
            await c.apply(review, review.nonce, "admin")
            transaction = (await store.read())["transaction"]
            await c.poll("proxy.example", 64)
            self.assertEqual((await store.read())["transaction"], transaction)
            self.assertIsNone((await store.read())["approval"])
        asyncio.run(scenario())

    def test_exact_reviewed_config_required_and_x_forwarded_not_changed(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            review = await c.preview("proxy.example", 64, "admin")
            backend.stable["cors_allowed_origins"] = ["https://changed.example"]
            with self.assertRaisesRegex(PolicyError, "HTTP settings changed"):
                await c.apply(review, review.nonce, "admin")
            self.assertEqual(backend.calls, [])
            fresh = await c.preview("proxy.example", 64, "admin")
            await c.apply(fresh, fresh.nonce, "admin")
            self.assertTrue(backend.calls[0]["use_x_forwarded_for"])
            self.assertEqual(backend.calls[0]["cors_allowed_origins"], ["https://changed.example"])
        asyncio.run(scenario())

    def test_preexisting_unmanaged_candidate_is_not_adopted(self):
        async def scenario():
            backend = FakeBackend({"trusted_proxies": ["2001:db8::/64"],
                                   "use_x_forwarded_for": True, "server_port": 8123})
            store = MemoryStore()
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            review = await c.preview("proxy.example", 64, "admin")
            self.assertTrue(review.already_trusted)
            with self.assertRaisesRegex(PolicyError, "not app-managed"):
                await c.apply(review, review.nonce, "admin")
            self.assertEqual(backend.calls, [])
            self.assertIsNone((await store.read()).get("managed_entry"))
        asyncio.run(scenario())

    def test_forwarded_header_trust_disabled_blocks_without_mutating(self):
        async def scenario():
            backend = FakeBackend({"trusted_proxies": [], "use_x_forwarded_for": False,
                                   "server_port": 8123})
            store = MemoryStore()
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            with self.assertRaisesRegex(PolicyError, "enable it in HA explicitly"):
                await c.preview("proxy.example", 64, "admin")
            self.assertEqual(backend.calls, [])
            self.assertFalse(backend.stable["use_x_forwarded_for"])
        asyncio.run(scenario())

    def test_two_prefix_changes_then_rollback_clears_transaction(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            addresses = ["2001:db8:1::1"]
            c = Coordinator(backend, store, lambda _: async_value(addresses))
            first = await c.preview("proxy.example", 64, "admin")
            await c.apply(first, first.nonce, "admin")
            backend.confirm()
            self.assertEqual(await c.observe_recovery(), "confirmed_by_home_assistant")
            saved = await store.read()
            self.assertEqual(saved["managed_network"], "2001:db8:1::/64")
            self.assertIsNone(saved["transaction"])

            addresses[:] = ["2001:db9:1::1"]
            second = await c.preview("proxy.example", 48, "admin")
            await c.apply(second, second.nonce, "admin")
            result = await c.poll("proxy.example", 48)
            self.assertEqual(result.status, "awaiting_user_confirmation")
            self.assertTrue((await store.read())["transaction"]["observed_trial"])
            backend.pending = None
            backend.active_type = "stable"
            result = await c.poll("proxy.example", 48)
            self.assertEqual(result.status, "rolled_back_after_observed_trial")
            saved = await store.read()
            self.assertIsNone(saved["transaction"])
            self.assertIsNone(saved["approval"])
            self.assertEqual(saved["managed_network"], "2001:db8:1::/64")
            self.assertEqual(saved["last_outcome"], "rolled_back_after_observed_trial")
        asyncio.run(scenario())

    def test_uncertain_disconnect_delayed_acceptance_never_resends(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            backend.accept_then_disconnect = True
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            review = await c.preview("proxy.example", 64, "admin")
            with self.assertRaisesRegex(PolicyError, "outcome uncertain"):
                await c.apply(review, review.nonce, "admin")
            self.assertEqual(len(backend.calls), 1)

            # A stable source config immediately after a timeout is not proof of
            # rollback: HA may accept the delayed request after this read.
            backend.pending = None
            backend.active_type = "stable"
            result = await c.poll("proxy.example", 64)
            self.assertEqual(result.status, "configure_outcome_requires_manual_inspection")
            self.assertIsNotNone((await store.read())["transaction"])
            self.assertFalse((await store.read())["transaction"]["observed_trial"])
            with self.assertRaisesRegex(PolicyError, "no retry is safe"):
                await c.preview("proxy.example", 64, "admin")

            # Later transaction-specific evidence permits safe attribution.
            backend.pending = {**backend.calls[0], "created_at": "metadata", "error": None}
            backend.active_type = "pending"
            result = await c.poll("proxy.example", 64)
            self.assertEqual(result.status, "awaiting_user_confirmation")
            self.assertTrue((await store.read())["transaction"]["observed_trial"])
            self.assertEqual(len(backend.calls), 1)
            backend.confirm()
            result = await c.poll("proxy.example", 64)
            self.assertEqual(result.status, "confirmed_by_home_assistant")
            state = await store.read()
            self.assertIsNone(state["transaction"])
            self.assertEqual(state["managed_network"], "2001:db8::/64")
            self.assertEqual(len(backend.calls), 1)
        asyncio.run(scenario())

    def test_external_mutation_clears_attribution_but_requires_new_review(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            review = await c.preview("proxy.example", 64, "admin")
            await store.write({**await store.read(), "transaction": {
                "id": "test", "phase": "configure_uncertain",
                "source_fingerprint": review.source_fingerprint,
                "source_active_type": "stable", "candidate_fingerprint": "wrong",
                "candidate_active_type": "pending", "target_network": review.candidate,
                "target_entry": review.candidate, "previous_managed_network": None,
                "previous_managed_entry": None, "observed_trial": True}, "approval": None})
            backend.stable["server_port"] = 9000
            self.assertEqual(await c.observe_recovery(), "external_http_configuration_change")
            state = await store.read()
            self.assertIsNone(state["transaction"])
            self.assertIsNone(state.get("managed_entry"))
            new_review = await c.preview("proxy.example", 64, "admin")
            self.assertNotEqual(new_review.source_fingerprint, review.source_fingerprint)
        asyncio.run(scenario())

    def test_active_slot_mismatch_never_claims_candidate(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            review = await c.preview("proxy.example", 64, "admin")
            candidate_config = configure_payload(backend.stable)
            candidate_config["trusted_proxies"] = [*candidate_config["trusted_proxies"], review.candidate]
            await store.write({**await store.read(), "transaction": {
                "id": "slot-mismatch", "phase": "configure_uncertain",
                "source_fingerprint": review.source_fingerprint,
                "source_active_type": "stable", "candidate_fingerprint": config_fingerprint(candidate_config),
                "candidate_active_type": "pending", "target_network": review.candidate,
                "target_entry": review.candidate, "previous_managed_network": None,
                "previous_managed_entry": None}, "approval": None})
            backend.stable = candidate_config
            backend.active_type = "pending"
            # Candidate appears in the slot, but active_config_type does not attribute it.
            self.assertEqual(await c.observe_recovery(), "configure_outcome_requires_manual_inspection")
            state = await store.read()
            self.assertIsNotNone(state["transaction"])
            self.assertIsNone(state.get("managed_network"))
        asyncio.run(scenario())

    def test_existing_manual_pending_config_is_never_overwritten(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            backend.pending = {"trusted_proxies": ["198.51.100.0/24"], "error": None}
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            with self.assertRaisesRegex(PolicyError, "already exists"):
                await c.preview("proxy.example", 64, "admin")
            self.assertEqual(backend.pending["trusted_proxies"], ["198.51.100.0/24"])
            self.assertEqual(backend.calls, [])
        asyncio.run(scenario())

    def test_matching_failed_pending_config_resolves_journal_and_blocks_new_write(self):
        async def scenario():
            backend, store = FakeBackend(), MemoryStore()
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]))
            review = await c.preview("proxy.example", 64, "admin")
            await c.apply(review, review.nonce, "admin")
            backend.rollback()
            result = await c.poll("proxy.example", 64)
            self.assertEqual(result.status, "pending_configuration_failed")
            saved = await store.read()
            self.assertIsNone(saved["transaction"])
            self.assertEqual(saved["last_outcome"], "pending_configuration_failed")
            with self.assertRaisesRegex(PolicyError, "failed pending"):
                await c.preview("proxy.example", 64, "admin")
        asyncio.run(scenario())

    def test_expired_and_wrong_user_nonce_do_not_apply(self):
        async def scenario():
            backend, store, now = FakeBackend(), MemoryStore(), [1000.0]
            c = Coordinator(backend, store, lambda _: async_value(["2001:db8::1"]),
                            clock=lambda: now[0])
            review = await c.preview("proxy.example", 64, "admin")
            with self.assertRaises(PolicyError):
                await c.apply(review, review.nonce, "other")
            now[0] += 601
            with self.assertRaises(PolicyError):
                await c.apply(review, review.nonce, "admin")
            self.assertEqual(backend.calls, [])
        asyncio.run(scenario())


async def async_value(value):
    return list(value)


if __name__ == "__main__":
    unittest.main()
