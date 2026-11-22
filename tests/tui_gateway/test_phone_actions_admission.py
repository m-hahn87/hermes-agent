"""Native-action provenance from an authenticated dashboard admission to tool execution.

The canonical dashboard path is REAL here: ``AuthorityConnection.submit`` admits through the
production ledger (its first receipt is ``queued``), the drain claims that row, and
``gateway.session_ingress`` binds the origin for the in-process execution that runs the tool. No
origin is injected into the tool, no fake authority reports ``started``, and no model, device or
live socket is touched — only a synthetic identity and a fake writer.

``test_prompt_submit_carries_the_originating_socket_into_the_turn_thread`` (original) covers the
standalone sidecar dispatch -> turn worker boundary; its fail-closed behaviour is kept there.
"""

import asyncio
import contextvars
import contextlib
import json
import threading
import time
import types

import pytest

from gateway import native_turn_origin, session_ingress
from gateway.session_contract import SessionRef
from gateway.session_controls import AuthorityConnection
from gateway.native_turn_origin import current_native_turn_origin
from hermes_state_runtime import claim_session_input, list_session_admissions
from tests.gateway.test_surface_admission import _authority
from tests.tools.test_phone_actions_v1 import _Socket
from tools import phone_control
from tools.approval_context import _approval_session_id
from tui_gateway import server
from tui_gateway import server_requests
from tui_gateway.transport import current_transport

_SID = "s"
_IDENTITY = {
    "user_id": "phone-user", "provider": "local", "profile_id": "p", "instance_id": "owner",
    "capabilities": ["session:read", "session:submit", "session:control"],
}


def test_prompt_submit_carries_the_originating_socket_into_the_turn_thread(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda session: True)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    sid = "phone-admission-sid"
    sock = _Socket()
    observed = []

    def observe_runner(rid, session_id, session, text, **kwargs):
        observed.append((session_id, text, kwargs.get("origin_transport"), current_transport()))

    monkeypatch.setattr(server, "_run_prompt_submit", observe_runner)
    ready = threading.Event()
    ready.set()
    session = {
        "agent": types.SimpleNamespace(session_id="phone-admission-key", clear_interrupt=lambda: None),
        "agent_ready": ready, "session_key": "phone-admission-key", "history": [],
        "history_lock": threading.Lock(), "history_version": 0, "running": False,
        "attached_images": [], "image_counter": 0, "cols": 80, "slash_worker": None,
        "show_reasoning": False, "tool_progress_mode": "all",
    }
    monkeypatch.setitem(server._sessions, sid, session)
    response = server.dispatch(  # pyright: ignore[reportAttributeAccessIssue] — installed by method context
        {"id": "p1", "method": "prompt.submit", "params": {"session_id": sid, "text": "list actions"}},
        transport=sock,
    )
    assert response and response["result"]["status"] == "streaming", response
    worker = session["_run_thread"]
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert observed == [(sid, "list actions", sock, None)]


@pytest.fixture(autouse=True)
def _clean():
    server_requests.reset_for_tests()
    token = _approval_session_id.set(_SID)
    yield
    _approval_session_id.reset(token)
    server_requests.reset_for_tests()


def _connection(authority, sock):
    return AuthorityConnection(authority, sock, dict(_IDENTITY))


async def _submitted(authority, conn, request_id="req-1", text="list actions"):
    """Resume, register phone actions v1 on the socket, and submit through the REAL admission."""
    await conn.dispatch({"id": 1, "method": "session.resume", "params": {"session_id": _SID}})
    server_requests.advertise_phone_actions(conn.transport, 1)
    reply = await conn.dispatch({"id": 2, "method": "prompt.submit", "params": {
        "session_id": _SID, "submission_id": request_id, "text": text}})
    assert reply["result"]["status"] == "queued", reply  # the canonical dashboard receipt
    return reply


def _queued_row(authority, request_id="req-1"):
    rows = list_session_admissions(authority.db, session_id=_SID)
    return next(row for row in rows if row["request_id"] == request_id)


def _claim_row(authority):
    """The drain's own claim step, run synchronously so the pending drain cannot race it."""
    return claim_session_input(authority.db, epoch=authority.epoch, session_id=_SID)


async def _stop_drain(authority):
    task = getattr(authority.sessions.get(_SID), "task", None)
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _answer_later(sock, result):
    def run():
        deadline = time.time() + 2
        frame = None
        while frame is None and time.time() < deadline:
            frame = next((f for f in sock.frames
                          if str(f.get("method", "")).startswith("phone.actions")), None)
            if frame is None:
                time.sleep(0.005)
        assert frame is not None, "phone action frame was never written"
        server_requests.resolve_response(
            {"jsonrpc": "2.0", "id": frame["id"], "result": result}, sock)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.asyncio
async def test_queued_dashboard_admission_reaches_phone_actions_with_the_origin_bound(
        tmp_path, monkeypatch):
    seen = {}

    async def answer(event):
        origin = current_native_turn_origin()
        seen["origin"] = origin
        seen["admission_id"] = event.message_id
        seen["result"] = json.loads(phone_control.phone_actions({"operation": "list"}))
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    sock = _Socket(dict(_IDENTITY))
    conn = _connection(authority, sock)
    server_requests.advertise_phone_actions(sock, 1)  # a real client advertises v1 before submitting
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 1.0)
    try:
        await _submitted(authority, conn)
        _answer_later(sock, {"status": "ok", "version": 1, "actions": [{"id": "alarm.set"}]})
        await asyncio.wait_for(authority.sessions[_SID].task, timeout=5)
    finally:
        await conn.close()

    origin = seen["origin"]
    assert origin is not None, "the queued admission's in-process execution must claim its origin"
    assert origin.transport is sock
    assert origin.admission_id == seen["admission_id"]
    assert origin.principal == "phone-user" and origin.session_id == _SID
    assert seen["result"] == {
        "status": "ok", "version": 1,
        "actions": [{"id": "alarm.set", **phone_control._ACTION_SPECS["alarm.set"]}]}
    # Contract wire: {status:…}-only answers, and the request carries no private provenance.
    frame = next(f for f in sock.frames if f.get("method") == "phone.actions.list")
    assert frame["method"] == "phone.actions.list"
    assert frame["params"] == {"session_id": _SID, "version": 1}
    assert set(seen["result"]) <= {"status", "version", "actions"}


@pytest.mark.asyncio
async def test_same_admission_executing_twice_binds_nothing_the_second_time(tmp_path, monkeypatch):
    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    conn = _connection(authority, _Socket(dict(_IDENTITY)))
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)
    try:
        await _submitted(authority, conn)
        ref = SessionRef("p", _SID)
        row = _claim_row(authority)
        first = session_ingress.claim_native_turn_origin(authority, ref, row)
        try:
            assert current_native_turn_origin() is not None
        finally:
            session_ingress.release_native_turn_origin(first)
        # A replay of the same admission (second claim) finds the entry already spent.
        second = session_ingress.claim_native_turn_origin(authority, ref, row)
        try:
            assert current_native_turn_origin() is None
            assert json.loads(phone_control.phone_actions({"operation": "list"})) == {
                "status": "unavailable"}
        finally:
            session_ingress.release_native_turn_origin(second)
    finally:
        await _stop_drain(authority)
        await conn.close()


@pytest.mark.asyncio
async def test_admission_executed_after_its_socket_closed_binds_nothing(tmp_path, monkeypatch):
    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    sock = _Socket(dict(_IDENTITY))
    conn = _connection(authority, sock)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)
    try:
        await _submitted(authority, conn)
        ref = SessionRef("p", _SID)
        row = _claim_row(authority)
        sock._closed = True  # the socket died while the turn was still queued
        token = session_ingress.claim_native_turn_origin(authority, ref, row)
        try:
            assert current_native_turn_origin() is None
            assert json.loads(phone_control.phone_actions({"operation": "list"})) == {
                "status": "unavailable"}
        finally:
            session_ingress.release_native_turn_origin(token)
        assert not [f for f in sock.frames if str(f.get("method", "")).startswith("phone.actions")]
    finally:
        await _stop_drain(authority)


@pytest.mark.asyncio
async def test_admission_executed_after_the_origin_ttl_binds_nothing(tmp_path, monkeypatch):
    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    conn = _connection(authority, _Socket(dict(_IDENTITY)))
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)
    try:
        await _submitted(authority, conn)
        # Deterministic expiry without sleeping: any positive elapsed age already exceeds a
        # negative TTL, so the redemption must refuse.
        monkeypatch.setattr(native_turn_origin, "ORIGIN_TTL_S", -1.0)
        ref = SessionRef("p", _SID)
        token = session_ingress.claim_native_turn_origin(authority, ref, _claim_row(authority))
        try:
            assert current_native_turn_origin() is None
            assert json.loads(phone_control.phone_actions({"operation": "list"})) == {
                "status": "unavailable"}
        finally:
            session_ingress.release_native_turn_origin(token)
    finally:
        await _stop_drain(authority)
        await conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("session", "other-session"),
    ("principal", "someone-else"),
])
async def test_another_session_or_principal_never_redeems_the_origin(
        tmp_path, monkeypatch, field, value):
    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    conn = _connection(authority, _Socket(dict(_IDENTITY)))
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)
    try:
        await _submitted(authority, conn)
        row = _claim_row(authority)
        ref = SessionRef("p", _SID)
        if field == "session":
            ref = SessionRef("p", value)
        else:
            row = dict(row, principal_id=value)
        token = session_ingress.claim_native_turn_origin(authority, ref, row)
        try:
            assert current_native_turn_origin() is None
            assert json.loads(phone_control.phone_actions({"operation": "list"})) == {
                "status": "unavailable"}
        finally:
            session_ingress.release_native_turn_origin(token)
    finally:
        await _stop_drain(authority)
        await conn.close()


@pytest.mark.asyncio
async def test_unregistered_admission_and_another_process_owner_bind_nothing(tmp_path, monkeypatch):
    """A synthesized/recovered turn and an owner that never saw the admission claim nothing."""
    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path / "first", monkeypatch, answer)
    conn = _connection(authority, _Socket(dict(_IDENTITY)))
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)
    try:
        await _submitted(authority, conn)
        row = _claim_row(authority)
    finally:
        await _stop_drain(authority)
        await conn.close()
    other = await _authority(tmp_path / "restarted", monkeypatch, answer)  # fresh in-memory owner
    ref = SessionRef("p", _SID)
    for owner, candidate in ((authority, dict(row, admission_id="never-admitted")), (other, row)):
        token = session_ingress.claim_native_turn_origin(owner, ref, candidate)
        try:
            assert current_native_turn_origin() is None
            assert json.loads(phone_control.phone_actions({"operation": "list"})) == {
                "status": "unavailable"}
        finally:
            session_ingress.release_native_turn_origin(token)


@pytest.mark.asyncio
async def test_a_managed_out_of_process_turn_does_not_redeem_the_origin(tmp_path, monkeypatch):
    from gateway import session_managed_worker

    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    conn = _connection(authority, _Socket(dict(_IDENTITY)))
    try:
        await _submitted(authority, conn)
        ref = SessionRef("p", _SID)
        row = _claim_row(authority)

        sentinel = object()

        async def admit_managed(*_args, **_kwargs):
            return sentinel

        monkeypatch.setattr(session_managed_worker, "managed_policy", lambda a, r: object())
        monkeypatch.setattr(session_managed_worker, "admit_managed", admit_managed)
        assert await session_ingress.execute_admission(authority, ref, row) is sentinel
        assert current_native_turn_origin() is None
        # The out-of-process turn redeemed nothing: the registration is still there for exactly
        # the admission it names (and only that one).
        token = session_ingress.claim_native_turn_origin(authority, ref, row)
        try:
            assert current_native_turn_origin() is not None
        finally:
            session_ingress.release_native_turn_origin(token)
    finally:
        await _stop_drain(authority)
        await conn.close()


def test_pinned_cancel_reaches_only_its_socket():
    sock, other = _Socket(), _Socket()
    emitted = []
    server_requests.bind_sinks(
        lambda frame: None, lambda event, sid, payload: emitted.append((event, sid, payload)),
        lambda sid: True, lambda sid: [sock, other],
        lambda event, sid, payload: {"jsonrpc": "2.0", "method": "event",
                                     "params": {"type": event, "session_id": sid,
                                                "payload": payload}})
    req = server_requests.ServerRequest("s", "phone.actions.list", {})
    req.transport = sock
    server_requests._emit_cancel(req, "timeout")
    assert emitted == []
    assert other.frames == []
    assert sock.frames[0]["params"]["type"] == "request.cancel"
    assert "payload" in sock.frames[0]["params"]


def test_bounded_register_distinguishes_unsent_from_uncertain():
    class _Bounded:
        def __init__(self, outcome):
            self.outcome = outcome
            self.frames = []

        def write(self, obj):
            self.frames.append(obj)
            return True

        def write_bounded(self, obj, deadline):
            assert deadline > 0
            return self.outcome

    expired = _Bounded(False)
    receipt = []
    assert server_requests.send("phone.actions.list", "s", {}, timeout=0.01,
                                transport=expired, deadline=time.monotonic() + 10,
                                dispatch_receipt=receipt) is None
    assert receipt == [False]  # proven unsent: no request frame ever reached the writer
    assert all(frame.get("method") != "phone.actions.list" for frame in expired.frames)

    uncertain = _Bounded(None)
    receipt = []
    server_requests.send("phone.actions.list", "s", {}, timeout=0.05,
                         transport=uncertain, deadline=time.monotonic() + 10,
                         dispatch_receipt=receipt)
    assert receipt == [True]  # enqueued: uncertain, never "unsent"

    late = _Bounded(True)
    receipt = []
    server_requests.send("phone.actions.list", "s", {}, timeout=0.01,
                         transport=late, deadline=time.monotonic() - 1,
                         dispatch_receipt=receipt)
    assert receipt == [False] and late.frames == []  # expired before any enqueue

@pytest.mark.asyncio
async def test_retired_origin_cannot_act_from_a_copied_context(tmp_path, monkeypatch):
    """A task/subagent created during the turn keeps a copy of the context: it must fail closed."""
    async def answer(event):
        return "ok"

    authority = await _authority(tmp_path, monkeypatch, answer)
    sock = _Socket(dict(_IDENTITY))
    conn = _connection(authority, sock)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)
    try:
        await _submitted(authority, conn)
        ref = SessionRef("p", _SID)
        row = _claim_row(authority)
        token = session_ingress.claim_native_turn_origin(authority, ref, row)
        seen = {}
        context = contextvars.copy_context()
        try:
            assert current_native_turn_origin() is not None
        finally:
            session_ingress.release_native_turn_origin(token)
        # The copy still holds the object the drained turn used; it must no longer be usable.
        frames_before = len(sock.frames)
        context.run(lambda: seen.update(result=phone_control.phone_actions({"operation": "list"})))
        assert json.loads(seen["result"]) == {"status": "unavailable"}
        assert len(sock.frames) == frames_before, "a retired origin must not even reach the wire"
    finally:
        await _stop_drain(authority)
        await conn.close()


@pytest.mark.asyncio
async def test_a_terminal_replay_never_rearms_the_origin(tmp_path, monkeypatch):
    from gateway.session_authority import AdmissionReceipt

    authority = await _authority(tmp_path, monkeypatch, lambda event: "ok")
    sock = _Socket(dict(_IDENTITY))
    conn = _connection(authority, sock)
    ref = SessionRef("p", _SID)
    terminal = AdmissionReceipt("adm-done", ref, 7, "terminal", "done", 1, 1)
    conn.remember_native_turn_origin(terminal, ref)
    # Nothing was armed by the terminal receipt, so the (single) claim binds nothing.
    token = session_ingress.claim_native_turn_origin(authority, ref, _row_of("adm-done"))
    try:
        assert current_native_turn_origin() is None, "a terminal replay must not arm an origin"
    finally:
        session_ingress.release_native_turn_origin(token)


def _row_of(admission_id):
    return {"admission_id": admission_id, "principal_id": "phone-user", "target_session_id": _SID}


@pytest.mark.asyncio
async def test_closing_the_socket_revokes_its_pending_origin(tmp_path, monkeypatch):
    authority = await _authority(tmp_path, monkeypatch, lambda event: "ok")
    sock = _Socket(dict(_IDENTITY))
    conn = _connection(authority, sock)
    try:
        reply = await _submitted(authority, conn)
        ref = SessionRef("p", _SID)
        admission_id = reply["result"]["admission_id"]
        await conn.close()
        token = session_ingress.claim_native_turn_origin(authority, ref, _row_of(admission_id))
        try:
            assert current_native_turn_origin() is None
        finally:
            session_ingress.release_native_turn_origin(token)
    finally:
        await _stop_drain(authority)

