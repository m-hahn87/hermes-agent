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
        _answer_later(origin, {"status": "ok", "version": 1, "actions": ["alarm.set", "timer.set"]})
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
        _answer_later(sock, {"status": "handed_off", "action": "alarm.set"})
        out = _run_tool(operation="run", action="alarm.set", arguments={"hour": 7, "minute": 30})
    finally:
        reset_turn_origin(token)

    assert out == {"status": "handed_off", "action": "alarm.set"}
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

    token = _origin(sock)
    try:
        out = _run_tool(operation="list")
    finally:
        reset_turn_origin(token)
    assert out["status"] == "unavailable"


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

    assert out.get("status") == "invalid_arguments" or "error" in out
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
