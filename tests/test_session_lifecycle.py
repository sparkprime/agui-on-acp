"""Integration tests for the Create / Connect / Prompt separation.

These exercise the three ``SessionManager`` operations (§2 of
``agui_on_acp_changes.md``) against a fake ACP agent driven over a real
JSON-RPC transport, asserting the central invariant: a conversation's id
never changes silently, and a failed resume/load is never papered over by
minting a new session.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import make_stack, teardown_stack
from tests.fake_agent import capabilities, end_turn, text, tool_end, tool_start, user_text
from tests.sse_helpers import read_sse_events

CWD = "/tmp/opencode"


def _prompt_body(sid: str, content: str = "hi") -> dict[str, Any]:
    return {
        "threadId": sid,
        "runId": "r1",
        "messages": [{"role": "user", "id": "u1", "content": content}],
        "forwardedProps": {"cwd": CWD},
    }


# ── Create ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_calls_new_session_and_returns_id():
    fake, manager, client = await make_stack()
    try:
        fake.script = [text("hi"), end_turn()]
        resp = await client.post("/ag-ui/sessions", json={"cwd": CWD})
        assert resp.status_code == 201
        sid = resp.json()["sessionId"]
        assert sid == "fake-session-1"
        assert len(fake.new_session_calls) == 1
        # Create never calls load/resume.
        assert fake.load_session_calls == []
        assert fake.resume_session_calls == []
    finally:
        await teardown_stack(fake, manager, client)


# ── Prompt (attach-only) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prompt_with_known_thread_id_never_calls_new_or_load():
    fake, manager, client = await make_stack()
    try:
        fake.script = [text("hi"), end_turn()]
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        async with client.stream("POST", "/ag-ui", json=_prompt_body(sid)) as resp:
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_FINISHED" for e in events)
        # Prompt path reuses the live session — no new/load/resume ACP call.
        assert len(fake.new_session_calls) == 1  # the create only
        assert fake.load_session_calls == []
        assert fake.resume_session_calls == []
        assert len(fake.prompt_calls) == 1
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_prompt_with_unknown_thread_id_and_resume_supported_calls_resume():
    """A threadId unknown to THIS manager (simulating a bridge restart) but
    present in the shared store resolves via ``session/resume`` — never
    ``session/new``."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(resume=True)
    )
    try:
        # Seed the store with a session created out-of-band (no live
        # ActiveSession — exactly the post-restart shape).
        stored = fake.store.create(CWD)
        sid = stored.session_id
        fake.script = [text("hi"), end_turn()]
        async with client.stream("POST", "/ag-ui", json=_prompt_body(sid)) as resp:
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_FINISHED" for e in events)
        assert len(fake.resume_session_calls) == 1
        assert fake.resume_session_calls[0]["session_id"] == sid
        # Critical regression guard: no new_session was minted.
        assert fake.new_session_calls == []
        assert fake.load_session_calls == []
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_prompt_with_unknown_thread_id_and_resume_unsupported_is_hard_error():
    """resume=False → 409, RUN_ERROR, and crucially NO new_session call."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(resume=False)
    )
    try:
        fake.script = [text("hi"), end_turn()]
        async with client.stream(
            "POST", "/ag-ui", json=_prompt_body("no-such-session")
        ) as resp:
            assert resp.status_code == 409
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_ERROR" for e in events)
        # The whole point: never fall back to create.
        assert fake.new_session_calls == []
        assert fake.load_session_calls == []
        assert fake.resume_session_calls == []
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_prompt_resume_against_truly_missing_session_yields_404_not_new_session():
    """resume=True but the id isn't in the store → 404, no new_session."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(resume=True)
    )
    try:
        fake.script = [text("hi"), end_turn()]
        async with client.stream(
            "POST", "/ag-ui", json=_prompt_body("truly-missing")
        ) as resp:
            assert resp.status_code == 404
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_ERROR" for e in events)
        assert fake.new_session_calls == []
        # resume_session was attempted and the agent raised resource_not_found.
        assert len(fake.resume_session_calls) == 1
    finally:
        await teardown_stack(fake, manager, client)


# ── Connect (replay) ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_kills_existing_live_session_instead_of_leaking():
    """Connecting to a session id that already has a live ActiveSession
    (e.g. created moments ago via ``POST /ag-ui/sessions``) must kill the
    old subprocess before adopting the new one — not orphan it.

    Regression for the connect_session() subprocess leak: it used to
    unconditionally spawn + overwrite ``self._sessions[session_id]`` with
    no kill of the prior entry, leaving the old subprocess running
    unreferenced until sweep_idle eventually reaped it.
    """
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        # Create → one live subprocess registered.
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        old_runner = active.runner
        assert old_runner.process is not None
        assert old_runner.conn is not None
        assert len(manager.sessions) == 1

        # Connect to the same id → spawns a fresh subprocess for the replay
        # and must kill the old one rather than orphaning it.
        new_active, _queue = await manager.connect_session(sid, CWD)

        # The old subprocess was killed (process + conn nulled by _fake_kill).
        assert old_runner.process is None
        assert old_runner.conn is None
        # Exactly one live entry remains — no duplicate, no orphan tracked.
        assert len(manager.sessions) == 1
        assert manager.sessions[sid] is new_active
        # The new subprocess is live.
        assert new_active.runner.process is not None
        # load_session ran on the new subprocess.
        assert len(fake.load_session_calls) == 1
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_calls_load_session_with_queue_already_attached(
    monkeypatch: pytest.MonkeyPatch,
):
    """The replay queue MUST be attached to the bridge BEFORE
    ``session/load`` runs, so the replay stream isn't dropped. Encodes the
    ordering-fix requirement (§4.3) directly."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        stored = fake.store.create(CWD)

        from agui_on_acp.bridge.acp_to_agui import AcpToAguiBridge

        # Record the order: start_replay must fire before load_session
        # completes (the manager calls start_replay THEN awaits load).
        order: list[str] = []
        _orig_start_replay = AcpToAguiBridge.start_replay

        def _spy_start_replay(self: AcpToAguiBridge, queue: Any) -> None:
            order.append("start_replay")
            _orig_start_replay(self, queue)

        monkeypatch.setattr(AcpToAguiBridge, "start_replay", _spy_start_replay)

        async with client.stream(
            "GET", f"/ag-ui/sessions/{stored.session_id}/connect?cwd={CWD}"
        ) as resp:
            assert resp.status_code == 200
            await read_sse_events(resp)
        # start_replay fired, and load_session ran on the agent.
        assert order == ["start_replay"], "start_replay must run before load"
        assert len(fake.load_session_calls) == 1
        assert fake.load_session_calls[0]["session_id"] == stored.session_id
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_replay_emits_messages_snapshot_including_user_turns():
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        stored = fake.store.create(CWD)
        stored.transcript = [
            user_text("please help"),
            text("sure thing"),
            tool_start("tc1", title="bash"),
            tool_end("tc1", raw_output="ok"),
            end_turn(),
        ]
        async with client.stream(
            "GET", f"/ag-ui/sessions/{stored.session_id}/connect?cwd={CWD}"
        ) as resp:
            assert resp.status_code == 200
            events = await read_sse_events(resp)
        snaps = [e for e in events if e["type"] == "MESSAGES_SNAPSHOT"]
        assert snaps, "expected a MESSAGES_SNAPSHOT from replay"
        msgs = snaps[0]["data"]["messages"]
        roles = [m["role"] for m in msgs]
        # The user turn MUST appear (the regression the design calls out).
        assert "user" in roles
        # The assistant reply is present too.
        assert "assistant" in roles
        # The tool call result is a tool-role message.
        assert "tool" in roles
        assert fake.load_session_calls[0]["session_id"] == stored.session_id
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_unsupported_loadSession_is_clear_error():
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=False)
    )
    try:
        stored = fake.store.create(CWD)
        async with client.stream(
            "GET", f"/ag-ui/sessions/{stored.session_id}/connect?cwd={CWD}"
        ) as resp:
            assert resp.status_code == 501
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_ERROR" for e in events)
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_unknown_session_is_404_not_500():
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        async with client.stream(
            "GET", f"/ag-ui/sessions/never-created/connect?cwd={CWD}"
        ) as resp:
            assert resp.status_code == 404
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_ERROR" for e in events)
        # No new_session was minted as a fallback.
        assert fake.new_session_calls == []
    finally:
        await teardown_stack(fake, manager, client)
