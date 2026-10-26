"""Fail-closed IPv6 proxy policy and durable approval transaction coordinator."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol


class PolicyError(Exception):
    """A safe, non-sensitive policy failure suitable for generic UI handling."""


HTTP_METADATA = {"created_at", "error", "error_message", "pending", "pending_error",
                "stable", "active_config_type", "revert_at", "default"}


def ingress_peer_allowed(peer: str | None) -> bool:
    try:
        return peer is not None and ipaddress.ip_address(peer) == ipaddress.ip_address("172.30.32.2")
    except ValueError:
        return False


def user_is_admin(users: list[dict[str, Any]], user_id: str) -> bool:
    return any(
        user.get("id") == user_id
        and user.get("is_active") is True
        and (user.get("is_owner") is True or "system-admin" in user.get("group_ids", []))
        for user in users
    )


def ipv6_networks(addresses: list[str], prefix: int) -> set[str]:
    if not 1 <= prefix <= 128:
        raise PolicyError("Invalid prefix length")
    if not addresses:
        raise PolicyError("No AAAA addresses returned")
    try:
        parsed = {ipaddress.IPv6Address(address) for address in addresses}
    except ValueError as err:
        raise PolicyError("Invalid AAAA response") from err
    return {str(ipaddress.IPv6Network((address, prefix), strict=False)) for address in parsed}


def single_candidate(addresses: list[str], prefix: int) -> str:
    networks = ipv6_networks(addresses, prefix)
    if len(networks) != 1:
        raise PolicyError("Multiple IPv6 networks returned")
    return next(iter(networks))


def config_fingerprint(config: dict[str, Any]) -> str:
    """Hash complete user HTTP settings after removing only HA-owned metadata."""
    normalized = {key: value for key, value in config.items() if key not in HTTP_METADATA}
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def configure_payload(config: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if key not in HTTP_METADATA}


class Backend(Protocol):
    async def http_config(self) -> dict[str, Any]: ...
    async def configure(self, config: dict[str, Any]) -> bool: ...


class Store(Protocol):
    async def read(self) -> dict[str, Any]: ...
    async def write(self, value: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class Preview:
    hostname: str
    prefix: int
    current: str | None
    candidate: str
    nonce: str
    user_id: str
    source_fingerprint: str
    source_active_type: str
    use_x_forwarded_for: bool
    trusted_proxies: tuple[str, ...]
    already_trusted: bool


@dataclass(frozen=True)
class PollResult:
    status: str
    hostname: str
    prefix: int
    current: str | None = None
    candidate: str | None = None


class Coordinator:
    """All state reads/transitions and HA decisions share one non-reentrant lock."""

    def __init__(self, backend: Backend, store: Store, resolver: Any, clock=time.time):
        self.backend = backend
        self.store = store
        self.resolver = resolver
        self.clock = clock
        self.lock = asyncio.Lock()

    async def poll(self, hostname: str, prefix: int) -> PollResult:
        """Inspect DNS/config and recover journal; never creates or invalidates approval."""
        async with self.lock:
            recovery = await self._observe_locked()
            state = await self.store.read()
            if state.get("transaction"):
                return PollResult(recovery, hostname, prefix)
            if recovery in {"confirmed_by_home_assistant", "rolled_back_after_observed_trial",
                            "pending_configuration_failed", "external_http_configuration_change"}:
                return PollResult(recovery, hostname, prefix)
            config = await self.backend.http_config()
            if config.get("pending"):
                status = "pending_configuration_failed" if config.get("pending_error") else "pending_configuration"
                return PollResult(status, hostname, prefix)
            candidate = single_candidate(await self.resolver(hostname), prefix)
            current = state.get("managed_network")
            if current is not None and state.get("managed_entry") not in config.get("trusted_proxies", []):
                return PollResult("managed_entry_missing", hostname, prefix, current, candidate)
            if candidate in config.get("trusted_proxies", []) and candidate != current:
                return PollResult("already_trusted_unmanaged", hostname, prefix, current, candidate)
            if candidate == current:
                return PollResult("unchanged", hostname, prefix, current, candidate)
            return PollResult("network_changed", hostname, prefix, current, candidate)

    async def preview(self, hostname: str, prefix: int, user_id: str) -> Preview:
        async with self.lock:
            recovery = await self._observe_locked()
            state = await self.store.read()
            if state.get("transaction"):
                if recovery == "configure_outcome_requires_manual_inspection":
                    raise PolicyError("Configure outcome remains uncertain; inspect HA settings; no retry is safe")
                raise PolicyError("A configuration transaction still needs attention")
            config = await self.backend.http_config()
            self._ensure_no_pending(config)
            if not config.get("use_x_forwarded_for", False):
                raise PolicyError("use_x_forwarded_for is disabled; enable it in HA explicitly before review")
            candidate = single_candidate(await self.resolver(hostname), prefix)
            old = state.get("managed_network")
            proxies = tuple(config.get("trusted_proxies", []))
            managed_entry = state.get("managed_entry")
            if managed_entry and managed_entry not in proxies:
                raise PolicyError("Previously managed entry is no longer active")
            already_trusted = candidate in proxies and candidate != managed_entry
            preview = Preview(
                hostname, prefix, old, candidate, secrets.token_urlsafe(32), user_id,
                config_fingerprint(config), str(config.get("active_config_type", "")),
                bool(config.get("use_x_forwarded_for", False)), proxies, already_trusted,
            )
            await self.store.write({**state, "approval": {
                "nonce": preview.nonce,
                "user_id": user_id,
                "hostname": hostname,
                "prefix": prefix,
                "current": old,
                "candidate": candidate,
                "source_fingerprint": preview.source_fingerprint,
                "source_active_type": preview.source_active_type,
                "use_x_forwarded_for": preview.use_x_forwarded_for,
                "trusted_proxies": list(proxies),
                "already_trusted": already_trusted,
                "created_at": self.clock(),
            }})
            return preview

    async def apply(self, preview: Preview, nonce: str, user_id: str) -> str:
        async with self.lock:
            await self._observe_locked()
            state = await self.store.read()
            if state.get("transaction"):
                raise PolicyError("A configuration transaction still needs attention")
            approval = state.get("approval") or {}
            if not self._approval_matches(preview, approval, nonce, user_id):
                raise PolicyError("Approval is stale or invalid")
            if preview.already_trusted:
                raise PolicyError("This address is already trusted but is not app-managed")
            if preview.current is not None and preview.candidate == preview.current:
                raise PolicyError("No network change needs applying")
            candidate = single_candidate(await self.resolver(preview.hostname), preview.prefix)
            if candidate != preview.candidate:
                raise PolicyError("DNS candidate changed; review again")
            current = await self.backend.http_config()
            self._ensure_no_pending(current)
            source_fingerprint = config_fingerprint(current)
            if (source_fingerprint != preview.source_fingerprint
                    or str(current.get("active_config_type", "")) != preview.source_active_type):
                raise PolicyError("HTTP settings changed; review the latest configuration")
            if not current.get("use_x_forwarded_for", False):
                raise PolicyError("use_x_forwarded_for is disabled; enable it in HA explicitly before review")
            if state.get("managed_network") != preview.current:
                raise PolicyError("Managed network changed; review again")
            if current.get("use_x_forwarded_for") and not current.get("trusted_proxies"):
                raise PolicyError("Forwarded-header trust is enabled without a trusted proxy")
            new_config = configure_payload(current)
            proxies = list(new_config.get("trusted_proxies", []))
            old_entry = state.get("managed_entry")
            if old_entry:
                if old_entry not in proxies:
                    raise PolicyError("Managed entry no longer exists")
                proxies.remove(old_entry)
            if preview.candidate in proxies:
                raise PolicyError("Candidate became externally trusted; review again")
            proxies.append(preview.candidate)
            new_config["trusted_proxies"] = proxies
            candidate_fingerprint = config_fingerprint(new_config)
            txid = secrets.token_urlsafe(18)
            transaction = {
                "id": txid,
                "phase": "configure_uncertain",
                "observed_trial": False,
                "source_fingerprint": source_fingerprint,
                "source_active_type": preview.source_active_type,
                "candidate_fingerprint": candidate_fingerprint,
                "candidate_active_type": "pending",
                "target_network": preview.candidate,
                "target_entry": preview.candidate,
                "previous_managed_network": state.get("managed_network"),
                "previous_managed_entry": state.get("managed_entry"),
            }
            await self.store.write({"managed_network": state.get("managed_network"),
                                    "managed_entry": state.get("managed_entry"),
                                    "transaction": transaction, "approval": None})
            try:
                restart = await self.backend.configure(new_config)
            except Exception as err:
                # The configure may have been accepted before the connection failed.
                # Never retry; the next read-only poll reconciles the write-ahead journal.
                raise PolicyError("Configure outcome uncertain; inspect HA settings") from err
            transaction["phase"] = "awaiting_confirmation"
            await self.store.write({"managed_network": state.get("managed_network"),
                                    "managed_entry": state.get("managed_entry"),
                                    "transaction": transaction, "approval": None})
            return "restart_requested" if restart else "pending_confirmation"

    async def observe_recovery(self) -> str:
        async with self.lock:
            return await self._observe_locked()

    async def _observe_locked(self) -> str:
        state = await self.store.read()
        tx = state.get("transaction")
        if not tx:
            return "idle"
        try:
            config = await self.backend.http_config()
        except Exception:
            return "configure_outcome_requires_manual_inspection"
        pending = config.get("pending")
        if isinstance(pending, dict) and pending.get("error"):
            if config_fingerprint(pending) == tx["candidate_fingerprint"]:
                await self._resolve(state, "pending_configuration_failed")
                return "pending_configuration_failed"
            return "external_pending_configuration"
        if pending:
            if (config_fingerprint(pending) == tx["candidate_fingerprint"]
                    and config.get("active_config_type") in {tx["source_active_type"],
                                                              tx["candidate_active_type"]}):
                tx["phase"] = "awaiting_confirmation"
                tx["observed_trial"] = True
                await self.store.write({**state, "transaction": tx})
                return ("awaiting_user_confirmation" if config.get("active_config_type") == "pending"
                        else "awaiting_home_assistant_trial")
            return "external_pending_configuration"
        stable_hash = config_fingerprint(config)
        active_type = config.get("active_config_type")
        if stable_hash == tx["candidate_fingerprint"] and active_type == "stable":
            resolved = {"managed_network": tx["target_network"],
                        "managed_entry": tx["target_entry"],
                        "last_outcome": "confirmed_by_home_assistant",
                        "transaction": None, "approval": None}
            await self.store.write(resolved)
            return "confirmed_by_home_assistant"
        if (stable_hash == tx["source_fingerprint"]
                and active_type == "stable"):
            if tx.get("observed_trial") is True:
                await self._resolve(state, "rolled_back_after_observed_trial")
                return "rolled_back_after_observed_trial"
            return "configure_outcome_requires_manual_inspection"
        if active_type == "stable" and tx.get("observed_trial") is True:
            await self._resolve(state, "external_http_configuration_change")
            return "external_http_configuration_change"
        return "configure_outcome_requires_manual_inspection"

    async def _resolve(self, state: dict[str, Any], outcome: str) -> None:
        tx = state.get("transaction") or {}
        await self.store.write({"managed_network": tx.get("previous_managed_network", state.get("managed_network")),
                                "managed_entry": tx.get("previous_managed_entry", state.get("managed_entry")),
                                "last_outcome": outcome, "transaction": None, "approval": None})

    def _approval_matches(self, preview: Preview, approval: dict[str, Any], nonce: str, user_id: str) -> bool:
        created = approval.get("created_at", 0)
        return all((
            secrets.compare_digest(preview.nonce, nonce),
            preview.user_id == user_id,
            approval.get("nonce") == nonce,
            approval.get("user_id") == user_id,
            approval.get("hostname") == preview.hostname,
            approval.get("prefix") == preview.prefix,
            approval.get("current") == preview.current,
            approval.get("candidate") == preview.candidate,
            approval.get("source_fingerprint") == preview.source_fingerprint,
            approval.get("source_active_type") == preview.source_active_type,
            approval.get("already_trusted") == preview.already_trusted,
            approval.get("use_x_forwarded_for") == preview.use_x_forwarded_for,
            tuple(approval.get("trusted_proxies", [])) == preview.trusted_proxies,
            0 <= self.clock() - created <= 600,
        ))

    @staticmethod
    def _ensure_no_pending(config: dict[str, Any]) -> None:
        if config.get("pending"):
            if config.get("pending_error"):
                raise PolicyError("Home Assistant has a failed pending HTTP configuration")
            raise PolicyError("A pending HTTP configuration already exists")
