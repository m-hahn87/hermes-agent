"""Client attribution is a label, never the session's routing source."""
import asyncio

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from gateway.session_client import client_problem


def test_sidecar_advertises_creator_client_support():
    response = server.handle_request({'id': 'cap', 'method': 'gateway.capabilities', 'params': {}})
    assert response['result']['session_client_v1'] is True


def test_client_validation():
    assert client_problem({'name': 'android', 'version': '1.0'}) is None
    for value in (None, {}, {'name': 'Android'}, {'name': 'android', 'extra': True},
                  {'name': 'android', 'version': None}, {'name': 'x' * 65}):
        assert client_problem(value) is not None


def test_create_persists_at_first_row_without_changing_source(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / 'state.db')
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_schedule_agent_build', lambda _: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_register_session_cwd', lambda _: None)
    try:
        result = server.handle_request({'id': 'c', 'method': 'session.create', 'params': {
            'source': 'desktop', 'client': {'name': 'android', 'version': '1.0'}}})
        assert 'result' in result, result
        sid = result['result']['session_id']
        key = result['result']['stored_session_id']
        assert db.get_session(key) is None
        assert server._ensure_session_db_row(server._sessions[sid])
        from gateway.session_client import stored_clients
        assert stored_clients(db, [key]) == {key: {'name': 'android', 'version': '1.0'}}
        assert db.get_session(key)['source'] == 'desktop'
    finally:
        db.close()


def test_seeded_branch_persists_its_creator(tmp_path, monkeypatch):
    from gateway.session_client import stored_clients
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('parent', source='desktop')
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_schedule_agent_build', lambda _: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_register_session_cwd', lambda _: None)
    sid = None
    try:
        response = server.handle_request({'id': 'c', 'method': 'session.create', 'params': {
            'source': 'desktop', 'client': {'name': 'android'}, 'parent_session_id': 'parent',
            'messages': [{'role': 'user', 'content': 'hello'}]}})
        assert 'result' in response, response
        sid = response['result']['session_id']
        key = response['result']['stored_session_id']
        assert stored_clients(db, [key]) == {key: {'name': 'android'}}
    finally:
        if sid:
            server._sessions.pop(sid, None)
        db.close()


def test_first_prompt_submit_persists_creator(tmp_path, monkeypatch):
    from gateway.session_client import stored_clients
    db = SessionDB(tmp_path / 'state.db')
    monkeypatch.setattr(server, '_get_db', lambda: db)
    monkeypatch.setattr(server, '_schedule_agent_build', lambda _: None)
    monkeypatch.setattr(server, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(server, '_register_session_cwd', lambda _: None)
    monkeypatch.setattr(server, '_start_agent_build', lambda *args: None)
    monkeypatch.setattr(server, '_restart_completed_failed_agent_build', lambda *args: False)
    monkeypatch.setattr(server, '_run_after_agent_ready', lambda *args: None)
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    real_thread = server.threading.Thread
    monkeypatch.setattr(server.threading, 'Thread', InlineThread)
    sid = None
    try:
        created = server.handle_request({'id': 'c', 'method': 'session.create', 'params': {
            'source': 'desktop', 'client': {'name': 'android'}}})['result']
        sid, key = created['session_id'], created['stored_session_id']
        assert db.get_session(key) is None
        submitted = server.handle_request({'id': 'p', 'method': 'prompt.submit', 'params': {
            'session_id': sid, 'text': 'first prompt'}})
        assert 'result' in submitted, submitted
        assert stored_clients(db, [key]) == {key: {'name': 'android'}}
        assert db.get_session(key)['source'] == 'desktop'
        assert [m['content'] for m in db.get_messages_as_conversation(key)] == ['first prompt']
        monkeypatch.setattr(server.threading, 'Thread', real_thread)
        from hermes_cli.web_routers import sessions
        monkeypatch.setattr(sessions, '_open_session_db_for_profile',
                            lambda *a, **kw: SessionDB(tmp_path / 'state.db', read_only=True))
        listed = sessions.get_sessions(limit=20, offset=0)['sessions']
        assert next(row for row in listed if row['id'] == key)['creator_client'] == {'name': 'android'}
        found = asyncio.run(sessions.search_sessions(q='first prompt'))['results']
        assert next(row for row in found if row['id'] == key)['creator_client'] == {'name': 'android'}
        old = server._sessions.pop(sid, None)  # cold resume, not the warm in-memory fast path
        if lease := old.get('active_session_lease'):
            lease.release()
        from tests.tui_gateway.test_resume_readonly_mount import _mount
        _mount(monkeypatch, db, tmp_path, tmp_path)
        resumed = server.handle_request({'id': 'r', 'method': 'session.resume', 'params': {
            'session_id': key, 'source': 'desktop'}})
        assert 'result' in resumed, resumed
        sid = resumed['result']['session_id']
        assert server._sessions[sid]['session_key'] == key
        monkeypatch.setattr(server.threading, 'Thread', InlineThread)
        continued = server.handle_request({'id': 'p2', 'method': 'prompt.submit', 'params': {
            'session_id': sid, 'text': 'follow up'}})
        assert 'result' in continued, continued
        monkeypatch.setattr(server.threading, 'Thread', real_thread)
        assert [m['content'] for m in db.get_messages_as_conversation(key)] == ['first prompt', 'follow up']
        assert stored_clients(db, [key]) == {key: {'name': 'android'}}
    finally:
        if sid:
            server._sessions.pop(sid, None)
        db.close()


def test_reject_invalid_client_on_wire():
    result = server.handle_request({'id': 'c', 'method': 'session.create', 'params': {
        'source': 'desktop', 'client': {'name': 'Android'}}})
    assert 'error' in result


def test_canonical_receipt_creator_projects_to_owned_lineage(tmp_path):
    import json
    from gateway.session_client import stored_clients
    from hermes_state_local import POLICY_PREFIX
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('owner', source='desktop', chat_id='owner')
    db.create_session('continuation', source='desktop', chat_id='owner')
    db.create_session('other', source='desktop', chat_id='owner')
    receipt = {'policy': {'request_json': json.dumps({'client': {'name': 'conduit'}})},
               'lineage': ['owner', 'continuation']}
    db._execute_write(lambda conn: conn.execute('INSERT INTO state_meta(key, value) VALUES (?, ?)',
                                                (POLICY_PREFIX + 'owner', json.dumps(receipt))))
    try:
        assert stored_clients(db, ['owner', 'continuation', 'other']) == {
            'owner': {'name': 'conduit'}, 'continuation': {'name': 'conduit'}}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_real_canonical_creation_receipt_projects_creator(tmp_path, monkeypatch):
    from gateway.session_client import stored_clients
    from hermes_state_local import local_receipt
    from tests.gateway.test_session_client_env import _home, _owner
    from tests.gateway.test_session_client_identity import _create
    home = _home(tmp_path)
    owner, connection = await _owner(home, monkeypatch)
    try:
        ref, _ = _create(owner, connection.actor, home, 'projection', client={'name': 'conduit'})
        assert 'policy' in local_receipt(owner.db, ref.session_id)
        owner._schedule = lambda ref: None
        attached = await connection.dispatch({'id': 1, 'method': 'session.resume', 'params': {'session_id': ref.session_id}})
        assert 'result' in attached, attached
        sent = await connection.dispatch({'id': 2, 'method': 'prompt.submit', 'params': {
            'session_id': ref.session_id, 'submission_id': 'first', 'text': 'hello'}})
        assert sent.get('result', {}).get('status') == 'queued', sent
        from hermes_state_runtime import claim_session_input
        claim = claim_session_input(owner.db, epoch=owner.epoch, session_id=ref.session_id)
        assert claim is not None
        assert stored_clients(owner.db, [ref.session_id]) == {ref.session_id: {'name': 'conduit'}}
    finally:
        await connection.close()
        owner.db.close()


def test_sidecar_compression_continuation_keeps_creator_without_marking_forks(tmp_path):
    from gateway.session_client import store_client, stored_clients
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('root', source='desktop')
        store_client(db, 'root', {'name': 'conduit'})
        db.end_session('root', 'compression')
        db.create_session('tip', source='desktop', parent_session_id='root')
        db.create_session('fork', source='tool', parent_session_id='root')
        db.create_session('old', source='desktop')
        assert db.get_compression_tip('root') == 'tip'
        assert stored_clients(db, ['tip', 'fork', 'old']) == {'tip': {'name': 'conduit'}}
    finally:
        db.close()


def test_old_database_and_first_creator_wins(tmp_path):
    from gateway.session_client import store_client, stored_clients
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('old', source='desktop')
    db.create_session('new', source='desktop')
    assert stored_clients(db, ['old', 'new']) == {}
    store_client(db, 'new', {'name': 'android'})
    store_client(db, 'new', {'name': 'desktop'})
    db.close()
    reader = SessionDB(tmp_path / 'state.db', read_only=True)
    try:
        assert stored_clients(reader, ['old', 'new']) == {'new': {'name': 'android'}}
    finally:
        reader.close()


def test_rest_list_detail_and_search_project_label(tmp_path, monkeypatch):
    from gateway.session_client import store_client
    from hermes_cli.web_routers import sessions
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('abc', source='desktop')
    db.append_message('abc', 'user', 'searchable needle')
    store_client(db, 'abc', {'name': 'android'})
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', lambda *a, **kw: SessionDB(tmp_path / 'state.db', read_only=True))
    try:
        listing = sessions.get_sessions(limit=20, offset=0)
        assert listing['sessions'][0]['creator_client'] == {'name': 'android'}
        detail = asyncio.run(sessions.get_session_detail('abc'))
        assert detail['creator_client'] == {'name': 'android'}
        search = asyncio.run(sessions.search_sessions(q='needle'))
        assert search['results'][0]['creator_client'] == {'name': 'android'}
        assert detail['source'] == 'desktop'
    finally:
        db.close()


def test_pre_canonical_read_only_database_projects_legacy_sessions(tmp_path):
    from gateway.session_client import stored_clients
    db = SessionDB(tmp_path / "old.db")
    db.create_session("old", source="desktop")
    db._execute_write(lambda conn: conn.execute("DROP TABLE state_meta"))
    db.close()
    reader = SessionDB(tmp_path / "old.db", read_only=True)
    try:
        assert stored_clients(reader, ["old"]) == {}
    finally:
        reader.close()


def test_creator_insert_failure_rolls_back_session(tmp_path):
    import sqlite3
    from gateway.session_client import stored_clients
    db = SessionDB(tmp_path / "atomic.db")
    db._execute_write(lambda conn: conn.execute("CREATE TABLE session_creator_clients (session_id TEXT PRIMARY KEY, client_json TEXT NOT NULL)"))
    db._execute_write(lambda conn: conn.execute("CREATE TRIGGER deny_creator BEFORE INSERT ON session_creator_clients BEGIN SELECT RAISE(ABORT, 'failure'); END"))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            db.create_session("new", source="desktop", creator_client={"name": "conduit"})
        assert db.get_session("new") is None
        assert stored_clients(db, ["new"]) == {}
    finally:
        db.close()


def test_profile_aggregate_includes_declared_creator(tmp_path, monkeypatch):
    from gateway.session_client import store_client
    from hermes_cli.web_routers import profiles
    db = SessionDB(tmp_path / 'profiles.db')
    db.create_session('one', source='desktop')
    store_client(db, 'one', {'name': 'conduit'})
    monkeypatch.setattr(profiles, '_profile_targets', lambda *a: [('default', tmp_path)])
    monkeypatch.setattr(profiles, '_read_profile_db', lambda name, home, errors, fn: fn(db))
    monkeypatch.setattr(profiles, '_corrupt_profile_stores', lambda targets: {})
    monkeypatch.setattr(profiles, 'subagent_listing_scope', lambda *a, **kw: (False, None))
    try:
        rows = profiles.get_profiles_sessions(limit=20, offset=0)['sessions']
        assert rows[0]['creator_client'] == {'name': 'conduit'}
        assert rows[0]['source'] == 'desktop'
    finally:
        db.close()
