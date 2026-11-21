"""phone_actions v1: one tool, strict args/results, and origin proved by the turn's own socket.

The contract (conduit-actions-v1) allows native actions only for a private authenticated dashboard
turn on exactly the socket that submitted it AND registered ``phone_actions_version: 1``. Never the
newest globally connected phone, never a relay/stdio fallback.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from tools import phone_control
from tools.approval_context import _approval_session_id
from tui_gateway import server_requests
from tui_gateway.transport import bind_turn_origin, reset_turn_origin


class _Socket:
    def __init__(self, identity=None):
        self.auth_identity = identity if identity is not None else {"user_id": "u1", "provider": "dashboard"}
        self.frames: list[dict] = []

    def write(self, obj):
        self.frames.append(obj)
        return True


@pytest.fixture(autouse=True)
def _clean():
    server_requests.reset_for_tests()
    token = _approval_session_id.set("s1")
    yield
    _approval_session_id.reset(token)
    server_requests.reset_for_tests()


def _origin(sock):
    token = bind_turn_origin(sock)
    return token


def _answer_later(sock, result):
    def run():
        deadline = time.time() + 2
        while not sock.frames and time.time() < deadline:
            time.sleep(0.005)
        assert sock.frames, "phone action frame was never written"
        server_requests.resolve_response(
            {"jsonrpc": "2.0", "id": sock.frames[-1]["id"], "result": result}, sock)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _run_tool(**kwargs):
    return json.loads(phone_control.phone_actions(kwargs))


def test_list_uses_the_originating_registered_socket_not_the_newest_phone(monkeypatch):
    origin, newer = _Socket(), _Socket()
    server_requests.advertise_phone_actions(origin, 1)
    server_requests.advertise_phone_actions(newer, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 1.0)

    token = _origin(origin)
    try:
        _answer_later(origin, {"status": "ok", "version": 1, "actions": [{"id": "alarm.set"}, {"id": "timer.set"}]})
        out = _run_tool(operation="list")
    finally:
        reset_turn_origin(token)

    assert out["status"] == "ok"
    assert [a["id"] for a in out["actions"]] == ["alarm.set", "timer.set"]
    assert newer.frames == []
    assert origin.frames[0]["method"] == "phone.actions.list"
    assert origin.frames[0]["params"] == {"session_id": "s1", "version": 1}


def test_run_sends_one_bounded_request(monkeypatch):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 1.0)

    token = _origin(sock)
    try:
        _answer_later(sock, {"status": "handed_off"})
        out = _run_tool(operation="run", action="alarm.set", arguments={"hour": 7, "minute": 30})
    finally:
        reset_turn_origin(token)

    assert out == {"status": "handed_off"}
    assert len(sock.frames) == 1
    assert sock.frames[0]["method"] == "phone.actions.run"
    assert sock.frames[0]["params"] == {
        "session_id": "s1", "version": 1, "action": "alarm.set",
        "arguments": {"hour": 7, "minute": 30}}


@pytest.mark.parametrize("status", ["totally_fine", "confirmed", "ok", {"unexpected": True}])
def test_unknown_client_result_is_never_success(monkeypatch, status):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 1.0)

    token = _origin(sock)
    try:
        _answer_later(sock, {"status": status, "secret": "must not escape"})
        out = _run_tool(operation="run", action="timer.set", arguments={"seconds": 60})
    finally:
        reset_turn_origin(token)

    assert out["status"] == "unknown_outcome"
    assert "secret" not in out


def test_fails_closed_without_registration_or_origin():
    sock = _Socket()
    token = _origin(sock)
    try:
        out = _run_tool(operation="list")
    finally:
        reset_turn_origin(token)
    assert out["status"] == "unavailable"

    server_requests.advertise_phone_actions(sock, 1)
    out = _run_tool(operation="list")  # no turn origin bound
    assert out["status"] == "unavailable"
    assert not sock.frames


def test_unauthenticated_or_legacy_schema_fails_closed():
    legacy = _Socket(identity=None)
    legacy.auth_identity = None
    server_requests.advertise_phone_actions(legacy, 1)
    token = _origin(legacy)
    try:
        assert _run_tool(operation="list")["status"] == "unavailable"
    finally:
        reset_turn_origin(token)

    old = _Socket()
    server_requests.advertise_phone_actions(old, 0)  # old client never sent phone_actions_version
    token = _origin(old)
    try:
        assert _run_tool(operation="run", action="timer.set",
                         arguments={"seconds": 5})["status"] == "unavailable"
    finally:
        reset_turn_origin(token)
    assert old.frames == []


def test_disconnect_revokes_registration_and_ends_pending(monkeypatch):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 1.0)
    token = _origin(sock)
    try:
        server_requests.forget(sock)
        assert _run_tool(operation="run", action="maps.open",
                         arguments={"destination": "Hauptbahnhof"})["status"] == "unavailable"
        assert sock.frames == []
    finally:
        reset_turn_origin(token)


@pytest.mark.parametrize("args", [
    {"operation": "run", "action": ["alarm.set"], "arguments": {}},
    {"operation": "run", "action": "rm -rf /", "arguments": {}},
    {"operation": "run", "action": "alarm.set", "arguments": {"hour": 24, "minute": 0}},
    {"operation": "run", "action": "alarm.set", "arguments": {"hour": 7}},
    {"operation": "run", "action": "timer.set", "arguments": {"seconds": True}},
    {"operation": "run", "action": "maps.open", "arguments": {"destination": "https://evil.example"}},
    {"operation": "run", "action": "maps.open",
     "arguments": {"destination": "x" * 9000}},
    {"operation": "run", "action": "maps.open",
     "arguments": {"destination": "ok", "package": "com.evil"}},
])
def test_invalid_input_never_reaches_the_phone(args):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    token = _origin(sock)
    try:
        out = _run_tool(**args)
    finally:
        reset_turn_origin(token)

    assert out.get("status") == "invalid_arguments"
    assert set(out) == {"status"}
    assert sock.frames == []


def test_run_timeout_is_unknown_outcome_with_no_replay(monkeypatch):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.05)

    token = _origin(sock)
    try:
        out = _run_tool(operation="run", action="timer.set", arguments={"seconds": 10})
    finally:
        reset_turn_origin(token)

    assert out["status"] == "unknown_outcome"
    assert len(sock.frames) == 1


@pytest.mark.parametrize("answer", [
    {"status": "ok", "version": 1, "actions": [{"id": "alarm.set", "extra": 1}]},
    {"status": "ok", "version": True, "actions": [{"id": "alarm.set"}]},
    {"status": "ok", "version": 2, "actions": [{"id": "alarm.set"}]},
    {"status": "ok", "version": 1, "actions": [{"id": "alarm.set"}] * 33},
    {"status": "ok", "version": 1, "actions": [{"id": "evil.action"}]},
    {"status": "ok", "version": 1, "actions": ["alarm.set"]},
    {"status": "ok", "version": 1, "actions": [{"id": "alarm.set"}], "extra": 1},
])
def test_list_success_is_strict(monkeypatch, answer):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.5)

    token = _origin(sock)
    try:
        _answer_later(sock, answer)
        out = _run_tool(operation="list")
    finally:
        reset_turn_origin(token)

    assert out == {"status": "unavailable"}


def test_list_error_carries_only_status(monkeypatch):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.5)

    token = _origin(sock)
    try:
        _answer_later(sock, {"status": "disabled"})
        out = _run_tool(operation="list")
    finally:
        reset_turn_origin(token)

    assert out == {"status": "disabled"}


def test_list_error_with_client_text_is_not_echoed(monkeypatch):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.5)

    token = _origin(sock)
    try:
        _answer_later(sock, {"status": "disabled", "error": "client supplied text"})
        out = _run_tool(operation="list")
    finally:
        reset_turn_origin(token)

    assert out == {"status": "unavailable"}


@pytest.mark.parametrize("answer", [
    {"status": "ok"},
    {"status": "confirmed"},
    {"status": "handed_off", "action": "alarm.set"},
    {"status": "handed_off", "error": "client supplied text"},
])
def test_run_accepts_only_plain_handoff_status(monkeypatch, answer):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.5)

    token = _origin(sock)
    try:
        _answer_later(sock, answer)
        out = _run_tool(operation="run", action="alarm.set", arguments={"hour": 7, "minute": 30})
    finally:
        reset_turn_origin(token)

    assert out == {"status": "unknown_outcome"}


def test_response_from_another_socket_never_settles_the_request(monkeypatch):
    origin, other = _Socket(), _Socket()
    server_requests.advertise_phone_actions(origin, 1)
    monkeypatch.setattr(phone_control, "PHONE_ACTIONS_TIMEOUT_S", 0.15)

    def answer():
        deadline = time.time() + 2
        while not origin.frames and time.time() < deadline:
            time.sleep(0.005)
        server_requests.resolve_response(
            {"jsonrpc": "2.0", "id": origin.frames[0]["id"], "result": {"status": "handed_off"}}, other)

    threading.Thread(target=answer, daemon=True).start()
    token = _origin(origin)
    try:
        out = _run_tool(operation="run", action="timer.set", arguments={"seconds": 10})
    finally:
        reset_turn_origin(token)

    assert out == {"status": "unknown_outcome"}


def test_native_requests_are_live_only_and_shutdown_releases_waiters(monkeypatch):
    sock = _Socket()
    server_requests.advertise_phone_actions(sock, 1)
    outcomes: list = []
    params = {"version": 1, "action": "timer.set", "arguments": {"seconds": 10}}

    thread = threading.Thread(
        target=lambda: outcomes.append(
            server_requests.send("phone.actions.run", "s1", params, timeout=None, transport=sock)),
        daemon=True)
    thread.start()
    deadline = time.time() + 2
    while not sock.frames and time.time() < deadline:
        time.sleep(0.005)

    assert sock.frames and sock.frames[0]["method"] == "phone.actions.run"
    # Live-only: the in-flight request is not offered to a reconnecting viewer.
    assert server_requests.open_requests("s1") == []
    assert server_requests.can_replay_request("phone.actions.list", {}) is False
    assert server_requests.can_replay_request("phone.actions.run", params) is False

    server_requests.cancel(None, reason="shutdown")  # normal process exit path
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert outcomes == [None]
    assert server_requests.open_requests("s1") == []
