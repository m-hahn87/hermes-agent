# Personal context in Live voice (2026-10-31)

Voice session creation now reuses Hermes MemoryStore sanitized snapshots for the
owning profile (USER.md and MEMORY.md). Each enabled block is bounded to 2400
characters; truncated blocks are marked. Snapshots are captured at creation only,
including reconnect creation, and do not mutate the running agent system prompt.
The existing config-scoped audio route supplies the home and secret scope.
External memory providers and older conversations remain available via delegation;
this startup snapshot is not an exhaustive view of all remembered information.

The delegation policy treats personal questions as memory lookups, even if phrased
as small talk. Missing local context must trigger backend lookup before a claim
of ignorance. Failed lookup must be described as a retrieval failure, not absence
of memory. Current corrections and fresh backend results supersede the snapshot.

The default deployment profile sets voice.gpt_live.instructions to Klaus,
German and informal du. This is deployment config, not a hardcoded identity for
other profiles. Credentials, model, voice, transport and Android wire contracts
are unchanged. No Android update is required.

Validation: scripts/run_tests.sh tests/tools/test_voice_live_memory.py
 tests/tui_gateway/test_voice_live_delegation.py — 7 passed. Both new behavioral
tests fail on the old implementation. Real profile-scoped worker execution covers
A -> B -> A, saved user/project facts, disabled user memory and bounded context.
The active deployment config was resolved without an external provider request;
Klaus/German instructions and both memory blocks were verified without printing
personal memory. The dashboard was restarted and is active; its unauthenticated
voice endpoint still returns 401. Live spoken behavior requires a new conversation
on the device and has not been verified by an automated audio roundtrip.
