"""Standalone prompt admission carries its submitting transport to the turn worker.

The conversation runner is the boundary here: this test exercises dispatch and the real
worker startup without running model, storage, or native action side effects.
"""

import threading
import types

from tests.tools.test_phone_actions_v1 import _Socket
from tui_gateway import server
from tui_gateway.transport import current_transport


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
