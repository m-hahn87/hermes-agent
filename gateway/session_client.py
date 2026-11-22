"""The client a local session's creator declared itself as (``session.create`` ``client``).

A third-party client names itself (``{"name": "my-client", "version": "1.2.0"}``) so a viewer or a
plugin can tell which client created the session. It is a self-declared label, never an
authentication: no policy, permission, toolset, approval, prompt or telemetry path reads it.

It rides the frozen creation request (``LocalSessionPolicy.request_json``) as sent, so it needs no new
column, receipt key or policy field: an older owner restores the session unchanged, and it is part of
the exact-retry identity (a retry naming another client is a different request).
"""
from __future__ import annotations

import json
import re

_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}')
_VERSION = re.compile(r'[!-~]{1,64}')  # printable ASCII, no whitespace


def client_problem(value) -> str | None:
    """Why *value* is not a client label (``None`` when it is one)."""
    if not isinstance(value, dict) or 'name' not in value or not set(value) <= {'name', 'version'}:
        return 'must be an object with a name and an optional version'
    if not isinstance(value['name'], str) or not _NAME.fullmatch(value['name']):
        return 'name must be 1-64 characters of a-z, 0-9, ".", "_" or "-", starting with a letter or digit'
    if 'version' in value and (not isinstance(value['version'], str) or not _VERSION.fullmatch(value['version'])):
        return 'version must be 1-64 printable ASCII characters without spaces'
    return None


def policy_client(policy) -> dict | None:
    """A copy of the client label *policy*'s creation request carries, or ``None`` (none was sent,
    no local policy, or a stored value that is not a valid label)."""
    if policy is None:
        return None
    try:
        value = json.loads(policy.request_json).get('client')
    except (AttributeError, TypeError, ValueError):
        return None
    return dict(value) if client_problem(value) is None else None


def store_client(db, session_id: str, client: dict) -> None:
    """Record the first creator declaration without modifying routing state."""
    if problem := client_problem(client):
        raise ValueError(problem)

    def write(conn):
        conn.execute('CREATE TABLE IF NOT EXISTS session_creator_clients '
                     '(session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE, client_json TEXT NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO session_creator_clients VALUES (?, ?)',
                     (session_id, json.dumps(client, separators=(',', ':'))))
    db._execute_write(write)


def stored_clients(db, session_ids) -> dict:
    """Read declarations from the sidecar; pre-feature databases have no table."""
    ids = list(dict.fromkeys(s for s in session_ids if s))
    if not ids:
        return {}
    found = {}
    if db._read_one("SELECT name FROM sqlite_master WHERE type='table' AND name='session_creator_clients'"):
        for start in range(0, len(ids), 500):
            batch = ids[start:start + 500]
            marks = ','.join('?' for _ in batch)
            for row in db._read_all(f'SELECT session_id, client_json FROM session_creator_clients WHERE session_id IN ({marks})', batch):
                try:
                    value = json.loads(row['client_json'])
                except (TypeError, ValueError):
                    continue
                if client_problem(value) is None:
                    found[row['session_id']] = value
        # A compressed sidecar chat has a new physical id. Only follow the DB's
        # recognized compression lineage, never a generic parent/chat_id edge.
        if hasattr(db, 'get_compression_lineage'):
            for sid in ids:
                if sid in found:
                    continue
                lineage = db.get_compression_lineage(sid)
                if sid not in lineage or not lineage or lineage[0] == sid:
                    continue
                row = db._read_one('SELECT client_json FROM session_creator_clients WHERE session_id=?', (lineage[0],))
                if row:
                    try:
                        value = json.loads(row['client_json'])
                    except (TypeError, ValueError):
                        continue
                    if client_problem(value) is None:
                        found[sid] = value
    # Canonical sessions freeze the declaration in the creation receipt. A continuation's
    # chat_id alone is insufficient evidence: local_lineage_owner checks receipt membership.
    if hasattr(db, '_read_ctx'):
        from hermes_state_local import POLICY_PREFIX, local_lineage_owner
        with db._read_ctx() as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_meta'").fetchone():
                return found
            for sid in ids:
                if sid in found:
                    continue
                owner = local_lineage_owner(conn, sid)
                row = conn.execute('SELECT value FROM state_meta WHERE key=?', (POLICY_PREFIX + owner,)).fetchone()
                if not row:
                    continue
                try:
                    policy = json.loads(row[0])['policy']
                    value = json.loads(policy['request_json']).get('client')
                except (TypeError, ValueError, KeyError, AttributeError):
                    continue
                if client_problem(value) is None:
                    found[sid] = value
    return found
