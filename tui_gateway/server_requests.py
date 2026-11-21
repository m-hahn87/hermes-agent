"""Server→client JSON-RPC requests: the backend asks the renderer a question and waits for the
response frame carrying the same ``id``.

JSON-RPC is peer-to-peer; this is the backend's half. Every "ask the renderer" bridge (clarify,
approval, sudo, secret, vault prompts, desktop GUI reads, MCP setup consent, the tour) is one
:func:`send` (blocking) or :func:`send_async` (queue-backed approvals) and one response frame from
the client — no paired ``*.request`` notification / ``*.respond`` method, no per-kind ``*.expire``.

Ids are ``srq-<12 hex>``: strings never collide with client-minted integer ids, and the random
part keeps a compute-host child's requests distinct from the parent's when both reach one socket.
A request that times out or is cancelled (interrupt, session close, shutdown) emits ONE
``request.cancel {id, method, reason}`` notification so every renderer tears the card down the
same way. A response for an id that is no longer open is dropped — the wait already returned.

Reconnect: unanswered requests are returned as ``open_requests`` by ``session.resume`` /
``session.activate`` / ``session.events.since`` (:func:`open_requests`); the shared TypeScript
channel re-delivers them as if they had just arrived, so the notification replay ring never
has to carry "a question still waiting for an answer".
Vault preview operations are live-only: their script payload is discarded after
dispatch and is never included in these reconnect snapshots.

Batch clarify keeps per-question locks (``clarify.lock`` → :func:`lock_answer`): answers stay
editable until every question is locked, locked answers survive a timeout, and the last lock
resolves the request with the full answer set.

Window-owned bridges (``preview.read`` / ``preview.act`` / ``terminal.read`` / ``window.read`` /
``tour``) are answered only by the window showing the session; every other attached window declines
with :data:`NOT_SHOWN_CODE`. A decline does not settle the request — the owner may still answer — until
every answering client attached to the session has declined; then the request resolves with
:data:`NOT_SHOWN_MESSAGE` at once instead of the agent waiting out the deadline (#119333).

Capability: a client says once per connection that it answers server→client requests
(``client.capabilities {server_requests: true}`` → :func:`advertise`). A WebSocket client that never
did is a build older than this half of the protocol — it drops the frame silently and the agent
would wait the full deadline (clarify's 300s) for nothing — so :func:`send` / :func:`send_async`
return the same ``None`` an error response produces without writing the frame (#112548).
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Callable

from tools.clarify_batch import settle_batch

logger = logging.getLogger(__name__)


class ServerRequest:
    __slots__ = (
        "answered",
        "created_at",
        "declined",
        "event",
        "id",
        "locked",
        "method",
        "on_result",
        "params",
        "qids",
        "replayable",
        "result",
        "sid",
        "transport",
        "withdrawn",
        "writing",
    )

    def __init__(self, sid: str, method: str, params: dict, *, qids: list[str] | None = None,
                 on_result: Callable[[dict | None], None] | None = None) -> None:
        self.id = f"srq-{uuid.uuid4().hex[:12]}"
        self.sid = sid
        self.method = method
        self.params = dict(params)
        self.replayable = can_replay_request(method, params)
        self.event = threading.Event()
        self.result: dict | None = None
        self.answered = False
        self.created_at = time.time()
        # Batch clarify: question ids still to lock, and the answers locked so far.
        self.qids = list(qids) if qids else None
        self.locked: dict[str, str | None] = {}
        self.on_result = on_result
        # Client transports that answered NOT_SHOWN_CODE (no window there shows this session).
        self.declined: set = set()
        # Set = the frame goes to this one client socket and only a response from it settles the request.
        self.transport: Any = None
        # Published but frame not yet written; a cancel in that gap parks its reason in ``withdrawn`` for _register.
        self.writing = False
        self.withdrawn: str | None = None

    def frame(self) -> dict:
        return {"jsonrpc": "2.0", "id": self.id, "method": self.method,
                "params": {"session_id": self.sid, **self.params}}

    def snapshot(self) -> dict:
        """``open_requests`` entry: the request as sent, plus the batch answers locked so far so a
        reconnecting client restores its ✓ state."""
        params = {"session_id": self.sid, **self.params}
        if self.locked:
            params["answers"] = dict(self.locked)
        return {"id": self.id, "method": self.method, "params": params}


_lock = threading.Lock()
_open: dict[str, ServerRequest] = {}


_LIVE_ONLY_REQUESTS = frozenset({"phone.actions.list", "phone.actions.run"})


def can_replay_request(method: str, params: dict) -> bool:
    """Secure page scripts belong only to the original live request, never a resume.
    Native phone-action requests are live-only too: their frame/params must never enter
    ``open_requests``, resume snapshots, compute-host replay or diagnostic state."""
    if method in _LIVE_ONLY_REQUESTS:
        return False
    return not (method == "preview.act" and params.get("action") == "vault")

# Frame sinks, bound by ``bind_sinks`` from server.py at import time (like the method_ctx split
# modules): importing server back from here would pick a different module object under the test
# fixtures that patch ``sys.modules`` around the server import.
_write: Callable[[dict], Any] = lambda frame: None
_emit: Callable[[str, str, dict], Any] = lambda event, sid, payload: None
# ``answerable(sid)``: False only when every client attached to the session is a build that never
# advertised handling server→client requests (session_transports.py::_session_client_answers_requests).
_answerable: Callable[[str], bool] = lambda sid: True
# ``clients(sid)``: the attached client transports that answer server→client requests — the set whose
# unanimous NOT_SHOWN_CODE decline settles a window-owned request (session_transports.py).
_clients: Callable[[str], list] = lambda sid: []

# Error code a client answers when none of its windows shows the request's session, and the refusal the
# tool reports once every attached client said so. Mirrored in apps/desktop server-requests.ts.
NOT_SHOWN_CODE = 4404
NOT_SHOWN_MESSAGE = ("No Hermes Desktop window is showing this chat, so its preview, terminal and tour are out "
                     "of reach. Ask the user to open this chat in the Desktop app, then retry.")

# Client transports that sent ``client.capabilities {server_requests: true}`` (identity set: StdioTransport
# has __slots__ and cannot be weak-referenced; ws.py forgets a peer on disconnect).
_answering_clients: set = set()


def bind_sinks(write_json: Callable[[dict], Any], emit: Callable[[str, str, dict], Any],
               answerable: Callable[[str], bool], clients: Callable[[str], list] | None = None) -> None:
    global _write, _emit, _answerable, _clients
    _write, _emit, _answerable = write_json, emit, answerable
    if clients is not None:
        _clients = clients


def advertise(transport: Any, server_requests: bool) -> None:
    """Record whether *transport*'s client answers server→client requests (``client.capabilities``)."""
    with _lock:
        if server_requests:
            _answering_clients.add(transport)
        else:
            _answering_clients.discard(transport)


# Transports whose client advertised ``phone_control`` (Conduit app): the phone.* server requests
# are pinned to these only, so a Desktop window attached to the same session can never settle a
# phone request with a method-not-found error while the phone is still working. Ordered by the last
# advertisement: a reinstalled app reconnects while its previous process may still hold a socket, and
# the newest advertisement is the build the user is actually looking at.
_phone_transports: dict[Any, float] = {}


def advertise_phone(transport: Any, phone_control: bool) -> None:
    """Record whether *transport*'s client controls the user's phone (``client.capabilities``)."""
    with _lock:
        if phone_control:
            _phone_transports[transport] = time.time()
        else:
            _phone_transports.pop(transport, None)


def phone_transport() -> Any | None:
    """The most recently advertising phone-control client (Conduit), or ``None``."""
    with _lock:
        if not _phone_transports:
            return None
        return max(_phone_transports.items(), key=lambda item: item[1])[0]


# Transports that registered native actions v1 (``client.capabilities {phone_actions_version: 1}``).
# Registration is per-socket: it is NOT a session/global phone pointer, so another phone that
# connected later can never run an action for a turn it did not submit. Disconnect revokes it.
_phone_action_transports: dict[Any, int] = {}


def advertise_phone_actions(transport: Any, version: Any) -> None:
    """Record (or clear) *transport*'s native-actions v1 registration. Anything but 1 is no registration."""
    with _lock:
        if type(version) is int and version == 1:
            _phone_action_transports[transport] = 1
        else:
            _phone_action_transports.pop(transport, None)


def phone_actions_registered(transport: Any) -> bool:
    """True only for the exact socket that negotiated native actions v1."""
    if transport is None:
        return False
    with _lock:
        return _phone_action_transports.get(transport) == 1


def forget(transport: Any) -> None:
    """Drop a disconnected transport's advertisement and end its pinned pending requests."""
    with _lock:
        _answering_clients.discard(transport)
        _phone_transports.pop(transport, None)
        _phone_action_transports.pop(transport, None)
    # A pinned native-action request must not outlive its socket: the tool sees None and reports
    # unknown_outcome (never a retry), and no reconnecting client inherits the card.
    cancel(None, "disconnect", transport=transport)


def answers_requests(transport: Any) -> bool:
    with _lock:
        return transport in _answering_clients


def _unanswerable(method: str, sid: str) -> bool:
    if _answerable(sid):
        return False
    logger.info("server request %s for %s not sent: the attached client predates server→client requests "
                "(update the Hermes app)", method, sid)
    return True


def _emit_cancel(req: ServerRequest, reason: str) -> None:
    _emit("request.cancel", req.sid, {"id": req.id, "method": req.method, "reason": reason})


def _register(req: ServerRequest) -> bool:
    """Publish and write *req*; returns whether the frame reached the transport's writer.
    ``False`` is the only proof the request was never dispatched (peer already gone)."""
    from tui_gateway.contracts import registry as contracts

    contract = contracts.SERVER_REQUESTS.get(req.method)
    if contract is None:
        raise RuntimeError(f"server request {req.method!r} has no contract in tui_gateway/contracts")
    _, problem = contracts.validate_params(contract, {"session_id": req.sid, **req.params})
    if problem is not None:
        raise ValueError(problem)  # a key the renderer's typed handler would never read: our bug
    frame = req.frame()
    if not req.replayable:
        # Build the immediate wire frame first; pending state keeps only correlation
        # and cancellation metadata, never the secret-bearing expression.
        req.params = {}
    with _lock:
        req.writing = True
        _open[req.id] = req
    delivered = False
    try:
        delivered = (req.transport.write if req.transport is not None else _write)(frame) is not False
    finally:
        with _lock:
            req.writing = False
            withdrawn = req.withdrawn
    if withdrawn is not None:
        # cancel() landed between publish and write: its request.cancel must follow the frame, not precede it.
        _emit_cancel(req, withdrawn)
    return delivered


def send(method: str, sid: str, params: dict, *, timeout: float | None,
         qids: list[str] | None = None, transport: Any = None,
         deadline: float | None = None, dispatch_receipt: list | None = None) -> dict | None:
    """Send one request and block for the response ``result`` (a dict).

    Returns ``None`` when the renderer never answered (timeout, cancel, or an error response — e.g.
    a client without a handler for ``method``). ``timeout`` semantics: None → wait until answered or
    cancelled, 0 → return immediately, > 0 → bounded wait. A batch (``qids``) that settled returns
    ``{"answers": <locked so far>, "outcome"}`` (``submitted`` / ``cancelled`` / ``timed_out``).
    ``transport`` pins a session-less request to the client socket that asked for it (an RPC handler
    calling back into its own caller's window): the frame goes only there, only its response settles
    it, and any error from it (a decline included) is a refusal.

    ``deadline`` is an absolute ``time.monotonic()`` bound for the WHOLE end-to-end send: an expired
    deadline never writes (the request is not queued to start later), and the wait is bounded by
    what remains after dispatch. ``dispatch_receipt`` (a list) receives ``True`` when the frame
    reached the writer and ``False`` only when it provably did not — the native-action tool uses
    that to distinguish ``unavailable`` from ``unknown_outcome`` without ever retrying.
    """
    if deadline is not None and time.monotonic() >= deadline:
        if dispatch_receipt is not None:
            dispatch_receipt.append(False)
        return None
    if transport is None and _unanswerable(method, sid):
        if dispatch_receipt is not None:
            dispatch_receipt.append(False)
        return None
    req = ServerRequest(sid, method, params, qids=qids)
    req.transport = transport
    if dispatch_receipt is not None:
        dispatch_receipt.append(_register(req))
    else:
        _register(req)
    wait_for = timeout
    if deadline is not None:
        remaining = max(0.0, deadline - time.monotonic())
        wait_for = remaining if timeout is None else min(timeout, remaining)
    try:
        req.event.wait(wait_for)
    except BaseException:
        # The wait itself died (KeyboardInterrupt, SystemExit, injected error): withdraw the request
        # or it stays in _open forever — replayed to every reconnecting client and reported by
        # pending_kind() as a human still being waited on.
        with _lock:
            still_open = _open.pop(req.id, None) is req
        if still_open:
            _emit_cancel(req, "interrupted")
        raise
    with _lock:
        # The verdict is the state committed under the lock, never wait()'s return value: a
        # response frame can land after the deadline expires and before this removal, and
        # settlement (resolve_response / lock_answer / cancel) already popped it (#112548).
        timed_out = _open.pop(req.id, None) is req
        answered, result, locked = req.answered, req.result, dict(req.locked)
    if answered:
        return result
    if timed_out:
        _emit_cancel(req, "timeout")
        if req.qids is not None:
            return settle_batch(locked, ended="timed_out")
    return None


def send_async(method: str, sid: str, params: dict, on_result: Callable[[dict | None], None]) -> Callable[[str], None]:
    """Send one request whose wait is owned elsewhere (the approval queue's own timeout). ``on_result``
    runs on the dispatching thread when the response lands. Returns ``settle(reason)``: call it when
    the underlying wait ends; if the request is still open it is withdrawn with ``request.cancel``."""
    if _unanswerable(method, sid):
        on_result(None)
        return lambda reason: None
    req = ServerRequest(sid, method, params, on_result=on_result)
    _register(req)

    def settle(reason: str) -> None:
        with _lock:
            still_open = _open.pop(req.id, None) is not None
        if still_open:
            _emit_cancel(req, reason)

    return settle


def _is_not_shown(frame: dict) -> bool:
    error = frame.get("error")
    return isinstance(error, dict) and error.get("code") == NOT_SHOWN_CODE


def _decline(rid: str, transport: Any) -> bool:
    """One client's "no window here shows this session". Settles only once every answering client
    attached to the session declined: a bystander window must not beat the owner (#113348), and with
    no owner at all the agent gets the refusal now rather than at the deadline (#119333). A decline
    from an unknown transport (relayed, proxied) is recorded nowhere and the wait goes on."""
    with _lock:
        req = _open.get(rid)
    if req is None:
        logger.debug("server request %s: decline dropped, request no longer open", rid)
        return False
    clients = set(_clients(req.sid)) if transport is not None else set()
    with _lock:
        if _open.get(rid) is not req:
            return True  # settled meanwhile
        if transport is not None:
            req.declined.add(transport)
        if not clients or not clients <= req.declined:
            return True
        _open.pop(rid, None)
        req.result = {"value": json.dumps({"success": False, "error": NOT_SHOWN_MESSAGE})}
        req.answered = True
    if req.on_result is not None:
        req.on_result(req.result)
    req.event.set()
    return True


def resolve_response(frame: dict, transport: Any = None) -> bool:
    """Route one client response frame to its open request. False when nothing is waiting for that id
    (already timed out / cancelled, or owned by another process — see the compute-host bridge).
    *transport* is the client connection the frame arrived on (counts a NOT_SHOWN_CODE decline)."""
    rid = frame.get("id")
    if not isinstance(rid, str):
        return False
    with _lock:
        pinned = _open.get(rid)
    if pinned is not None and pinned.transport is not None and pinned.transport is not transport:
        logger.debug("server request %s: response from a client it was not sent to dropped", rid)
        return False
    if _is_not_shown(frame) and (pinned is None or pinned.transport is None):
        return _decline(rid, transport)
    with _lock:
        req = _open.get(rid)
        if req is None:
            # Already settled (timed out, cancelled, answered from another surface) or owned by
            # another process; say so — a dropped answer used to vanish without a trace.
            logger.debug("server request %s: response dropped, request no longer open", rid)
            return False
        # Removing the request and committing its outcome are one settlement.
        # ``cancel()`` also settles under this lock, so the first side to get
        # here wins instead of a later cancellation overwriting a response.
        _open.pop(rid, None)
        if "error" in frame:
            req.result, req.answered = None, False
        else:
            result = frame.get("result")
            req.result = result if isinstance(result, dict) else {}
            if req.qids:
                # Batch clarify: answers locked early via clarify.lock belong to the final set even when
                # the closing response only carries the tail the user answered last (one rule with the
                # session owner's registry: ``tools.clarify_batch``).
                req.result = settle_batch(req.locked, req.result)
            req.answered = True
    if "error" in frame:
        error = frame["error"] if isinstance(frame["error"], dict) else {}
        if req.method in _LIVE_ONLY_REQUESTS:
            # No client-supplied error text in diagnostics, even at debug level.
            logger.warning("server request %s (%s) answered with error code %s",
                           rid, req.method, error.get("code"))
        else:
            logger.warning("server request %s (%s) answered with error %s: %s",
                           rid, req.method, error.get("code"), error.get("message"))
    if req.on_result is not None:
        req.on_result(req.result)
    req.event.set()
    return True


def lock_answer(request_id: str, question_id: str, answer: str | None) -> list[str] | None:
    """Lock one batch-clarify answer (update-in-place; ``None`` = skipped). Returns the question ids
    still unanswered; the last lock resolves the request with the full ``{"answers"}`` set. ``None``
    when no open batch has that id (expired or foreign); ``ValueError`` for an unknown question id."""
    with _lock:
        req = _open.get(request_id)
        if req is None or req.qids is None:
            return None
        if question_id not in req.qids:
            raise ValueError(f"unknown question_id {question_id!r}")
        req.locked[question_id] = answer
        remaining = [qid for qid in req.qids if qid not in req.locked]
        if not remaining:
            req.result, req.answered = settle_batch(req.locked, ended="submitted"), True
            _open.pop(request_id, None)
    if not remaining:
        req.event.set()
    return remaining


def cancel(sid: str | None = None, reason: str = "interrupted", *, method: str | None = None,
           transport: Any = None) -> int:
    """Withdraw open requests — only *sid*'s (session.interrupt must not touch other sessions'), or
    every one when *sid* is None (shutdown); *method* narrows it to one request kind and *transport*
    to requests pinned to one client socket (disconnect). Blocked waits
    return None (a batch returns its locked answers with ``outcome: cancelled``); queue-backed requests
    run ``on_result(None)`` so their owner can settle. Returns the number withdrawn."""
    with _lock:
        targets = [req for req in _open.values()
                   if (sid is None or req.sid == sid) and (method is None or req.method == method)
                   and (transport is None or req.transport is transport)]
        for req in targets:
            _open.pop(req.id, None)
            if req.qids is not None:
                req.result, req.answered = settle_batch(req.locked, ended="cancelled"), True
            else:
                req.result, req.answered = None, False
            if req.writing:
                req.withdrawn = reason
        announced = {req.id for req in targets if not req.writing}
    for req in targets:
        if req.on_result is not None:
            req.on_result(None)
        req.event.set()
        if req.id in announced:
            _emit_cancel(req, reason)
    return len(targets)


def open_requests(sid: str) -> list[dict]:
    """Unanswered requests for *sid*, oldest first."""
    with _lock:
        reqs = sorted((req for req in _open.values() if req.sid == sid and req.replayable),
                      key=lambda r: r.created_at)
    return [req.snapshot() for req in reqs]


def open_request_count() -> int:
    """Unanswered server→client requests across every session: the process is waiting on a
    human (clarify, approval, sudo, secret, ...) and must not be treated as idle."""
    with _lock:
        return len(_open)


def pending_kind(sid: str) -> str:
    """Method of the oldest open request for *sid* ("" when none) — the session is waiting on a human."""
    with _lock:
        reqs = [req for req in _open.values() if req.sid == sid]
    return min(reqs, key=lambda r: r.created_at).method if reqs else ""


def resolve_pinned_response(frame: Any, transport: Any) -> bool:
    """Settle *frame* when it answers a request pinned to *transport* (``send(transport=...)``); False for
    anything else, which stays with its usual owner (a session authority's own registry, or the
    unpinned path of :func:`resolve_response`)."""
    if not is_response_frame(frame) or not isinstance(frame.get("id"), str):
        return False
    with _lock:
        req = _open.get(frame["id"])
    return req is not None and req.transport is not None and req.transport is transport and resolve_response(
        frame, transport)


def is_response_frame(obj: Any) -> bool:
    """A client response: has an ``id`` and a ``result``/``error`` member but no ``method``."""
    return isinstance(obj, dict) and "method" not in obj and "id" in obj and ("result" in obj or "error" in obj)


def reset_for_tests() -> None:
    with _lock:
        _open.clear()
        _answering_clients.clear()
        _phone_transports.clear()
        _phone_action_transports.clear()
