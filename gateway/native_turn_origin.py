"""Ephemeral provenance for ONE native-action turn admitted on an authenticated dashboard socket.

The Conduit contract (conduit-actions-v1 §1) allows ``phone.actions.*`` only for a private,
authenticated dashboard turn submitted on exactly the WebSocket that registered v1. Routing
labels, IPs, client params and "the newest connected phone" are never authority.

This module carries the server-attested association from the authenticated admission
(``AuthorityConnection.submit`` / ``session_queued_controls.replace_queued``) to the in-process
execution of that same admission (``session_ingress.execute_admission``): authenticated principal,
profile, canonical session id, the exact negotiated socket, and the specific admission id. It is:

* in-memory only — never written to the durable admission payload, RPC params, session state,
  renderer events, IPC or audit;
* registered at admission for EVERY dashboard submission of the socket, keyed by the admission id
  the ledger returned — including the canonical ``queued`` receipt the drain claims later;
* one-shot — the first execution of that admission id spends the entry, so a replay finds nothing;
* short-lived — an entry nobody claims expires after :data:`ORIGIN_TTL_S`;
* exact-match — principal, profile, session, admission id, socket liveness and the socket's
  authenticated identity are all re-checked at redemption (``session_ingress.claim_native_turn_origin``).

Registration is the claim ticket, not authority: only an in-process execution of this very admission
claims it, and a managed/out-of-process worker, a recovered, replayed or synthesized turn never
reaches the claim.
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
import time
from typing import Any

#: How long an unclaimed origin may sit in the registry. A queued turn the same socket admitted runs
#: from this process's drain right after admission; anything older never belonged to that execution.
ORIGIN_TTL_S = 120.0

_REGISTRY_ATTR = "_native_turn_origins"


class _Lifecycle:
    """Mutable, in-process lifecycle of one origin; frozen dataclass fields stay immutable."""

    __slots__ = ("revoked",)

    def __init__(self) -> None:
        self.revoked = False


@dataclass(frozen=True)
class NativeTurnOrigin:
    """Server-attested association for one admitted dashboard turn. Never serialized."""

    principal: str
    profile_id: str
    session_id: str
    admission_id: str
    transport: Any  # the negotiated authenticated socket; in-process only
    lifecycle: _Lifecycle = field(default_factory=_Lifecycle)

    @property
    def revoked(self) -> bool:
        return self.lifecycle.revoked


_current_origin: ContextVar[NativeTurnOrigin | None] = ContextVar(
    "hermes_native_turn_origin", default=None)


class _Registry:
    __slots__ = ("entries", "lock")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        # admission_id -> (monotonic registration time, origin)
        self.entries: dict[str, tuple[float, NativeTurnOrigin]] = {}


def _registry(authority, *, create: bool = False) -> _Registry | None:
    registry = getattr(authority, _REGISTRY_ATTR, None)
    if registry is None and create:
        registry = _Registry()
        setattr(authority, _REGISTRY_ATTR, registry)
    return registry


def register_native_turn_origin(authority, origin: NativeTurnOrigin) -> None:
    """Record *origin* for the admission id the ledger returned. Call only for an authenticated
    submission of this socket; the redemption rules are applied by the claim, never here."""
    registry = _registry(authority, create=True)
    assert registry is not None
    with registry.lock:
        now = time.monotonic()
        for key in [k for k, (at, _o) in registry.entries.items() if now - at > ORIGIN_TTL_S]:
            registry.entries.pop(key, None)
        registry.entries[origin.admission_id] = (now, origin)


def pop_registered_origin(authority, admission_id) -> tuple[float, NativeTurnOrigin] | None:
    """Remove and return ``(registered_at, origin)`` for one admission id.

    One-shot (the entry is spent however the redemption verdict turns out) and deliberately dumb:
    every condition that makes the association safe is re-checked by the single caller,
    ``session_ingress.claim_native_turn_origin``.
    """
    registry = _registry(authority)
    if registry is None or not isinstance(admission_id, str) or not admission_id:
        return None
    with registry.lock:
        return registry.entries.pop(admission_id, None)


def revoke_native_turn_origins(authority, *, session_id: str | None = None,
                               transport: Any = None) -> int:
    """Drop pending origins on stop/disconnect/retirement. Returns how many were revoked."""
    registry = _registry(authority)
    if registry is None:
        return 0
    with registry.lock:
        doomed = [key for key, (_at, origin) in registry.entries.items()
                  if (session_id is None or origin.session_id == session_id)
                  and (transport is None or origin.transport is transport)]
        for key in doomed:
            registry.entries.pop(key, None)
    return len(doomed)


def bind_native_turn_origin(origin: NativeTurnOrigin | None):
    """Bind the claimed origin to the executing turn's context (tools inherit it)."""
    return _current_origin.set(origin)


def reset_native_turn_origin(token) -> None:
    """Unbind the executing turn and retire the origin object itself.

    A task or subagent created during the turn keeps a COPY of the context and would otherwise keep
    reading the (spent) origin after the turn ended. Retiring the object makes every such copy fail
    closed, so no post-turn work can act as the user's phone lease.
    """
    origin = _current_origin.get()
    if origin is not None:
        origin.lifecycle.revoked = True
    _current_origin.reset(token)


def current_native_turn_origin() -> NativeTurnOrigin | None:
    """The claimed origin of the turn executing in this context, or ``None``."""
    return _current_origin.get()
