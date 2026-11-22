"""Loopback WebSocket + HTTP regression for Conduit's capability-gated client label.

Uses the real legacy sidecar, WS dispatch and browse routers; no live Hermes state or LLM.
The old-server capability response is simulated at the WS boundary.
"""
import asyncio
import json
import socket

import httpx
import pytest
import uvicorn
import websockets
from fastapi import FastAPI, WebSocket

from hermes_state import SessionDB
from tests.gateway.test_session_client_env import _home, _owner


@pytest.mark.asyncio
@pytest.mark.parametrize('old_server,old_client', [(None, False), ('missing', False), ('legacy', False), (None, True)])
async def test_android_wire_browse_and_old_server_fallback(tmp_path, monkeypatch, old_server, old_client):
    home = _home(tmp_path)
    owner, connection = await _owner(home, monkeypatch)
    from hermes_cli.web_routers import sessions
    from tui_gateway import ws as ws_mod, server as sidecar
    monkeypatch.setattr(sidecar, '_get_db', lambda: owner.db)
    monkeypatch.setattr(sidecar, '_schedule_agent_build', lambda _: None)
    monkeypatch.setattr(sidecar, '_schedule_session_cap_enforcement', lambda: None)
    monkeypatch.setattr(sidecar, '_register_session_cwd', lambda _: None)
    monkeypatch.setattr(sidecar, '_start_agent_build', lambda *args: None)
    monkeypatch.setattr(sidecar, '_restart_completed_failed_agent_build', lambda *args: False)
    monkeypatch.setattr(sidecar, '_run_after_agent_ready', lambda *args: None)
    monkeypatch.setattr(sessions, '_open_session_db_for_profile',
                        lambda *a, **kw: SessionDB(owner.db.db_path, read_only=True))
    monkeypatch.setattr(ws_mod, '_start_backend_liveness', lambda: None)
    original = ws_mod._dispatch_request

    async def dispatch(authority, req, method, transport):
        if old_server and method == 'gateway.capabilities':
            if old_server == 'legacy':
                return {'jsonrpc': '2.0', 'id': req['id'], 'result': {'per_session_exclusive_submit': True}}
            return {'jsonrpc': '2.0', 'id': req['id'], 'error': {'code': -32601, 'message': 'Method not found'}}
        return await original(authority, req, method, transport)
    monkeypatch.setattr(ws_mod, '_dispatch_request', dispatch)
    owner.db.create_session('legacy', source='desktop')
    owner.db.append_message('legacy', 'user', 'legacy needle')
    app = FastAPI()
    app.include_router(sessions.list_router)
    app.include_router(sessions.search_router)

    @app.websocket('/api/ws')
    async def ws_endpoint(ws: WebSocket):
        await ws_mod.handle_ws(ws, auth_identity={'user_id': 'owner'}, operator=True)

    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='warning', lifespan='off'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        async with websockets.connect(f'ws://127.0.0.1:{port}/api/ws') as wire:
            async def rpc(method, params, rid):
                await wire.send(json.dumps({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params}))
                while True:
                    frame = json.loads(await asyncio.wait_for(wire.recv(), 20))
                    if frame.get('id') == rid:
                        return frame
            cap = await rpc('gateway.capabilities', {}, 1)
            supports = cap.get('result', {}).get('session_client_v1') is True
            assert supports == (old_server is None), cap
            params: dict = {'source': 'desktop'}
            if supports and not old_client:
                params['client'] = {'name': 'conduit'}
            created = await rpc('session.create', params, 2)
            assert 'result' in created, created
            sid = created['result']['stored_session_id']
            submitted = await rpc('prompt.submit', {'session_id': created['result']['session_id'],
                                                    'text': 'new needle'}, 3)
            assert 'result' in submitted, submitted
            for _ in range(500):
                if owner.db.get_session(sid) and owner.db.get_messages_as_conversation(sid):
                    break
                await asyncio.sleep(.01)
            assert owner.db.get_messages_as_conversation(sid)
            async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}', trust_env=False) as http:
                listing = (await http.get('/api/sessions', params={'order': 'recent'})).json()['sessions']
                search = (await http.get('/api/sessions/search', params={'q': 'needle'})).json()['results']
                for rows in (listing, search):
                    by_id = {row['id']: row for row in rows}
                    assert by_id[sid]['source'] == 'desktop'
                    assert by_id['legacy']['source'] == 'desktop'
                    assert 'creator_client' not in by_id['legacy']
                    if old_server or old_client:
                        assert 'creator_client' not in by_id[sid]
                    else:
                        assert by_id[sid]['creator_client'] == {'name': 'conduit'}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 20)
        await connection.close()
        owner.db.close()
