"""Integration tests for the Create / Connect / Prompt separation.

These exercise the three ``SessionManager`` operations (§2 of
``agui_on_acp_changes.md``) against a fake ACP agent driven over a real
JSON-RPC transport, asserting the central invariant: a conversation's id
never changes silently, and a failed resume/load is never papered over by
minting a new session.
"""

import asyncio
import json
from typing import Any

import acp
import pytest
from acp import schema as acp_schema

from agui_on_acp.agui.events import AguiEvent, MessagesSnapshotEvent
from agui_on_acp.bridge.acp_to_agui import AcpToAguiBridge
from tests.conftest import make_stack, teardown_stack
from tests.fake_agent import (
    capabilities,
    end_turn,
    text,
    thought,
    tool_end,
    tool_start,
    user_text,
)
from tests.sse_helpers import read_sse_events

CWD = "/tmp/opencode"


def _prompt_body(sid: str, content: str = "hi") -> dict[str, Any]:
    # No ``cwd`` in forwardedProps — the bridge resolves it from its durable
    # ``session_id → cwd`` record, which is the whole point of the
    # cwd-persistence change these tests exercise.
    return {
        "threadId": sid,
        "runId": "r1",
        "messages": [{"role": "user", "id": "u1", "content": content}],
        "forwardedProps": {},
    }


# ── Create ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_calls_new_session_and_returns_id():
    """Create calls ``session/new`` and returns the agent-minted session id."""
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
    """A prompt on a live session reuses it — no new/load/resume ACP call."""
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
async def test_prompt_with_no_live_session_and_resume_supported_calls_resume():
    """No live ``ActiveSession`` (e.g. after a bridge restart that
    preserved the cwd record) but the bridge knows the cwd and the agent
    supports resume → ``session/resume`` is called, never ``session/new``."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(resume=True)
    )
    try:
        fake.script = [text("hi"), end_turn()]
        # Create the session (writes the bridge cwd record + a live
        # session), then drop the live session — modelling "the bridge
        # restarted; its on-disk cwd record survived but its in-memory
        # session didn't".
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        await manager.stop(sid)
        async with client.stream("POST", "/ag-ui", json=_prompt_body(sid)) as resp:
            events = await read_sse_events(resp)
        assert any(e["type"] == "RUN_FINISHED" for e in events)
        assert len(fake.resume_session_calls) == 1
        assert fake.resume_session_calls[0]["session_id"] == sid
        # Critical regression guard: prompt did not mint a new session —
        # the one new_session call is the explicit create above.
        assert len(fake.new_session_calls) == 1
        assert fake.load_session_calls == []
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_prompt_with_known_id_but_resume_unsupported_is_hard_error():
    """Bridge knows the cwd (store record exists) but the agent doesn't
    support ``session/resume`` and there's no live session → 409 JSON error,
    and crucially NO new_session call (never falls back to create)."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(resume=False)
    )
    try:
        fake.script = [text("hi"), end_turn()]
        # Create the session (writes the bridge cwd record + live session),
        # then drop the live session so attach_for_prompt must resume.
        active = await manager.create_session(cwd=CWD)
        await manager.stop(active.session_id)
        resp = await client.post("/ag-ui", json=_prompt_body(active.session_id))
        assert resp.status_code == 409
        assert "error" in resp.json()
        # Never fell back to create: only the explicit create_session call.
        assert len(fake.new_session_calls) == 1
        assert fake.load_session_calls == []
        assert fake.resume_session_calls == []  # resume unsupported → never called
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_prompt_against_truly_unknown_session_yields_404_not_new_session():
    """An id the bridge never recorded (no cwd record, no live session) →
    404, no new_session, and no subprocess spawned at all (resolve_cwd fails
    before attach_for_prompt runs)."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(resume=True)
    )
    try:
        fake.script = [text("hi"), end_turn()]
        resp = await client.post("/ag-ui", json=_prompt_body("truly-missing"))
        assert resp.status_code == 404
        assert "error" in resp.json()
        assert fake.new_session_calls == []
        # resume_session was NOT attempted — resolve_cwd failed first.
        assert fake.resume_session_calls == []
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
        # Create via the manager so the bridge's cwd record exists (the
        # client no longer needs to resend cwd on connect).
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id

        # Record the order: start_replay must fire before load_session
        # completes (the manager calls start_replay THEN awaits load).
        order: list[str] = []
        _orig_start_replay = AcpToAguiBridge.start_replay

        def _spy_start_replay(self: AcpToAguiBridge, queue: Any) -> None:
            order.append("start_replay")
            _orig_start_replay(self, queue)

        monkeypatch.setattr(AcpToAguiBridge, "start_replay", _spy_start_replay)

        # No ``?cwd=`` — the bridge resolves it from its durable record.
        async with client.stream("GET", f"/ag-ui/sessions/{sid}/connect") as resp:
            assert resp.status_code == 200
            await read_sse_events(resp)
        # start_replay fired, and load_session ran on the agent.
        assert order == ["start_replay"], "start_replay must run before load"
        assert len(fake.load_session_calls) == 1
        assert fake.load_session_calls[0]["session_id"] == sid
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_replay_emits_messages_snapshot_including_user_turns():
    """Replay emits a MESSAGES_SNAPSHOT including user, assistant and tool turns."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        # Script the fake agent's replay transcript (session/load re-emits
        # this history as session/update notifications).
        fake.store.sessions[sid].transcript = [
            user_text("please help"),
            text("sure thing"),
            tool_start("tc1", title="bash"),
            tool_end("tc1", raw_output="ok"),
            end_turn(),
        ]
        async with client.stream("GET", f"/ag-ui/sessions/{sid}/connect") as resp:
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
        assert fake.load_session_calls[0]["session_id"] == sid
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_replay_interleaves_reasoning_in_messages_snapshot():
    """Replayed reasoning appears interleaved in the MESSAGES_SNAPSHOT at the
    position it occurred, not concatenated into a single leading message.

    Regression: the bridge used to emit REASONING_* delta events during
    replay (ahead of the MESSAGES_SNAPSHOT, which excluded reasoning), so
    the ag-ui client preserved the streamed reasoning in place — rendering
    every thought concatenated at the top of the transcript. The fix
    coalesces AgentThoughtChunk into ``role="reasoning"`` SnapshotMessages
    in transcript order, and the snapshot carrying reasoning makes the
    client drop any streamed copy (see default.ts MESSAGES_SNAPSHOT handler).
    """
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        fake.store.sessions[sid].transcript = [
            user_text("do thing A and B"),
            thought("thinking about A"),
            text("doing A"),
            tool_start("tc1", title="bash"),
            tool_end("tc1", raw_output="ok"),
            thought("thinking about B"),
            text("doing B"),
            end_turn(),
        ]
        async with client.stream("GET", f"/ag-ui/sessions/{sid}/connect") as resp:
            assert resp.status_code == 200
            events = await read_sse_events(resp)
        snaps = [e for e in events if e["type"] == "MESSAGES_SNAPSHOT"]
        assert snaps, "expected a MESSAGES_SNAPSHOT from replay"
        msgs = snaps[0]["data"]["messages"]
        roles = [m["role"] for m in msgs]

        # Two distinct reasoning messages — NOT one concatenated block.
        reasoning = [m for m in msgs if m["role"] == "reasoning"]
        assert len(reasoning) == 2, f"expected 2 reasoning msgs, got {len(reasoning)}"
        assert reasoning[0]["content"] == "thinking about A"
        assert reasoning[1]["content"] == "thinking about B"

        # Ordering: user, reasoning(A), assistant(A, carries the tool call),
        # tool(result), reasoning(B), assistant(B). Each thought sits where
        # it occurred — not concatenated at the top.
        assert roles == [
            "user",
            "reasoning",
            "assistant",
            "tool",
            "reasoning",
            "assistant",
        ], f"unexpected ordering: {roles}"

        # No REASONING_* delta events should have been emitted during replay
        # — reasoning travels only in the snapshot now.
        reasoning_events = [e for e in events if e["type"].startswith("REASONING_")]
        assert (
            reasoning_events == []
        ), f"replay must not emit REASONING_* deltas: {reasoning_events}"
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_replay_tool_call_args_match_live_path():
    """Replayed tool-call arguments must match what the live path emits —
    only ``raw_input``, NOT ``kind``/``locations`` from ``ToolCallStart``.

    Previously the replay builder merged ``kind`` and ``locations`` from
    ``ToolCallStart`` into the args (and the live path did not), causing
    live and replay to show different JSON for the same tool call. Now
    both paths use only ``raw_input``, keeping the JSON consistent."""

    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        fake.store.sessions[sid].transcript = [
            user_text("do it"),
            tool_start(
                "tc1",
                title="read file",
                kind="read",
                locations=[{"path": "/a/b.txt", "line": 7}],
                raw_input={
                    "kind": "read",
                    "locations": [{"path": "/a/b.txt", "line": 7}],
                },
            ),
            tool_end("tc1", raw_output="ok"),
            end_turn(),
        ]
        async with client.stream("GET", f"/ag-ui/sessions/{sid}/connect") as resp:
            assert resp.status_code == 200
            events = await read_sse_events(resp)
        snaps = [e for e in events if e["type"] == "MESSAGES_SNAPSHOT"]
        assert snaps, "expected a MESSAGES_SNAPSHOT from replay"
        assistant_msgs: list[dict[str, Any]] = [
            m for m in snaps[0]["data"]["messages"] if m["role"] == "assistant"
        ]
        assert assistant_msgs, "expected an assistant message carrying the tool call"
        tool_calls: list[dict[str, Any]] = list(
            assistant_msgs[0].get("toolCalls") or []
        )
        assert tool_calls, "expected the tool call on the assistant message"
        # ``arguments`` is a JSON string; it must round-trip with the
        # locations as plain dicts (not pydantic objects — the original
        # regression), and must match exactly what the live path would
        # emit (just raw_input, no extra kind/locations from ToolCallStart).
        args = json.loads(str(tool_calls[0]["function"]["arguments"]))
        assert args["kind"] == "read"
        assert len(args["locations"]) == 1
        assert args["locations"][0]["path"] == "/a/b.txt"
        assert args["locations"][0]["line"] == 7
        # The tool name is NOT overwritten by the progress title — it stays
        # as the original ToolCallStart title (no "command" key → no suffix).
        assert tool_calls[0]["function"]["name"] == "read file"
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_replay_bash_tool_call_shows_command_in_name():
    """A replayed bash tool call displays as ``"bash: ls -la"`` (tool name +
    command), NOT just the command (which opencode sends as the progress
    ``title``). The args JSON contains only ``raw_input`` (``{command, cwd}``),
    matching the live path — no ``kind``/``locations``."""

    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        fake.store.sessions[sid].transcript = [
            user_text("list files"),
            tool_start(
                "tc1",
                title="bash",
                kind="execute",
                raw_input={"command": "ls -la", "cwd": "/tmp"},
            ),
            tool_end("tc1", raw_output="total 0"),
            end_turn(),
        ]
        async with client.stream("GET", f"/ag-ui/sessions/{sid}/connect") as resp:
            assert resp.status_code == 200
            events = await read_sse_events(resp)
        snaps = [e for e in events if e["type"] == "MESSAGES_SNAPSHOT"]
        assert snaps, "expected a MESSAGES_SNAPSHOT from replay"
        assistant_msgs = [
            m for m in snaps[0]["data"]["messages"] if m["role"] == "assistant"
        ]
        tool_calls = list(assistant_msgs[0].get("toolCalls") or [])
        assert tool_calls, "expected the tool call"
        # Display name includes the command: "bash: ls -la"
        assert tool_calls[0]["function"]["name"] == "bash: ls -la"
        # Args are just raw_input — no kind/locations
        args = json.loads(str(tool_calls[0]["function"]["arguments"]))
        assert args == {"command": "ls -la", "cwd": "/tmp"}
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_unsupported_load_session_is_clear_error():
    """Connecting when loadSession is unsupported returns a 501 error."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=False)
    )
    try:
        active = await manager.create_session(cwd=CWD)
        resp = await client.get(f"/ag-ui/sessions/{active.session_id}/connect")
        assert resp.status_code == 501
        assert "error" in resp.json()
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_connect_unknown_session_is_404_not_500():
    """Connecting to an unknown session id returns 404, not 500."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(load_session=True)
    )
    try:
        resp = await client.get("/ag-ui/sessions/never-created/connect")
        assert resp.status_code == 404
        assert "error" in resp.json()
        # No new_session was minted as a fallback.
        assert fake.new_session_calls == []
    finally:
        await teardown_stack(fake, manager, client)


# ── Dict-fallback replay regression ───────────────────────────────────────


@pytest.mark.asyncio
async def test_dict_fallback_replay_folds_tool_call_and_update():
    """Regression for the silently-dropped-dict-tool-call-update bug.

    The legacy ``_handle_session_update_dict`` had a replay-redirect guard
    that listed ``tool_call`` and ``tool_call_update`` in its ``kind in
    (...)`` tuple but only actually handled ``agent_message_chunk`` /
    ``user_message_chunk`` / ``turn_end`` inside it. A dict-shaped
    ``tool_call``/``tool_call_update`` arriving during a dict-fallback
    replay entered the guarded block, matched no inner ``elif``, and
    returned — silently swallowed with no snapshot entry.

    The convergence deletes that guard entirely: the dict handlers now
    call the same shared ``_process_*`` methods as the typed path, and
    ``_emit()`` folds the resulting events into the replay accumulator.
    This test drives the bridge directly with raw dict updates (the
    dict-fallback path) during replay and asserts the tool call and its
    result appear in the ``MESSAGES_SNAPSHOT`` — which would have failed
    against the pre-convergence code.
    """
    bridge = AcpToAguiBridge(task_id="t1")
    queue: asyncio.Queue[AguiEvent] = asyncio.Queue()
    bridge.start_replay(queue)

    # A dict-shaped tool_call + tool_call_update sequence (the
    # dict-fallback path — no typed schema objects involved).
    await bridge.session_update(
        "t1",
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "running bash"}},
    )
    await bridge.session_update(
        "t1",
        {"sessionUpdate": "tool_call", "toolCallId": "tc1", "title": "bash"},
    )
    await bridge.session_update(
        "t1",
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc1",
            "status": "in_progress",
            "raw_input": {"command": "ls -la", "cwd": "/tmp"},
        },
    )
    await bridge.session_update(
        "t1",
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc1",
            "status": "completed",
            "raw_output": {"output": "total 0"},
        },
    )
    await bridge.session_update("t1", {"sessionUpdate": "turn_end"})

    bridge.end_replay()

    # Drain the real queue — RUN_STARTED, MESSAGES_SNAPSHOT, RUN_FINISHED.
    events: list[AguiEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    types = [e.type.value for e in events]
    assert types[0] == "RUN_STARTED"
    assert "MESSAGES_SNAPSHOT" in types
    assert types[-1] == "RUN_FINISHED"

    snap = next(e for e in events if isinstance(e, MessagesSnapshotEvent))
    msgs = snap.messages
    roles = [m.role for m in msgs]
    assert roles == ["assistant", "tool"], f"unexpected roles: {roles}"

    assistant = msgs[0]
    assert assistant.content == "running bash"
    assert assistant.toolCalls is not None
    assert len(assistant.toolCalls) == 1
    call = assistant.toolCalls[0]
    assert call.id == "tc1"
    # Display name derived from tool name + command (live path behaviour
    # inherited by replay via the shared _process_tool_call_update).
    assert call.function["name"] == "bash: ls -la"
    assert json.loads(call.function["arguments"]) == {
        "command": "ls -la",
        "cwd": "/tmp",
    }

    tool_msg = msgs[1]
    assert tool_msg.role == "tool"
    assert tool_msg.toolCallId == "tc1"
    assert tool_msg.content == "total 0"


@pytest.mark.asyncio
async def test_dict_fallback_replay_user_message_chunk():
    """A dict-shaped ``user_message_chunk`` during replay coalesces into a
    ``role="user"`` ``SnapshotMessage`` (the legacy replay-redirect guard
    handled this case, but the convergence moves it to a dedicated
    ``_handle_user_message_chunk_dict`` shim — this locks that in)."""
    bridge = AcpToAguiBridge(task_id="t1")
    queue: asyncio.Queue[AguiEvent] = asyncio.Queue()
    bridge.start_replay(queue)

    await bridge.session_update(
        "t1",
        {"sessionUpdate": "user_message_chunk", "content": {"text": "please help"}},
    )
    await bridge.session_update(
        "t1",
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "sure"}},
    )
    await bridge.session_update("t1", {"sessionUpdate": "turn_end"})

    bridge.end_replay()

    events: list[AguiEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    snap = next(e for e in events if isinstance(e, MessagesSnapshotEvent))
    roles = [m.role for m in snap.messages]
    assert roles == ["user", "assistant"]
    assert snap.messages[0].content == "please help"
    assert snap.messages[1].content == "sure"


@pytest.mark.asyncio
async def test_replay_tool_call_with_raw_input_only_at_start_has_nonempty_args():
    """A tool call that carries its full ``raw_input`` at ``ToolCallStart``
    time (and never sends a ``ToolCallProgress`` with ``raw_input``) still
    shows non-empty args in the replay snapshot.

    The old replay path (``_append_replay_tool_start``) used
    ``ToolCallStart.raw_input`` as the initial args; the converged path
    inherits the live path's deferred-start behaviour, which originally
    discarded ``ToolCallStart.raw_input`` entirely — producing empty args
    for agents that send args only at start time. The fix stores
    ``ToolCallStart.raw_input`` as a fallback in ``_pending_tool_starts``
    and flushes it as ``TOOL_CALL_ARGS`` at completion when no
    ``ToolCallProgress.raw_input`` arrived first. This test locks that in
    for both the typed and dict paths.
    """

    for label, start_update in (
        (
            "typed",
            acp_schema.ToolCallStart(
                session_update="tool_call",
                tool_call_id="tc1",
                title="bash",
                status="pending",
                raw_input={"command": "ls -la", "cwd": "/tmp"},
            ),
        ),
        (
            "dict",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc1",
                "title": "bash",
                "raw_input": {"command": "ls -la", "cwd": "/tmp"},
            },
        ),
    ):
        bridge = AcpToAguiBridge(task_id="t1")
        queue: asyncio.Queue[AguiEvent] = asyncio.Queue()
        bridge.start_replay(queue)

        await bridge.session_update("t1", start_update)
        # Completion with raw_output only — NO ToolCallProgress with raw_input.
        await bridge.session_update(
            "t1",
            acp.schema.ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id="tc1",
                status="completed",
                raw_output={"output": "total 0"},
            ),
        )
        await bridge.session_update("t1", {"sessionUpdate": "turn_end"})
        bridge.end_replay()

        events: list[AguiEvent] = []
        while not queue.empty():
            events.append(queue.get_nowait())
        snap = next(e for e in events if isinstance(e, MessagesSnapshotEvent))
        assistant = next(m for m in snap.messages if m.role == "assistant")
        assert assistant.toolCalls is not None
        call = assistant.toolCalls[0]
        # Display name includes the command (derived from the start-time
        # raw_input fallback).
        assert call.function["name"] == "bash: ls -la", f"[{label}] name"
        # Args are non-empty — the fix.
        args = json.loads(call.function["arguments"])
        assert args == {"command": "ls -la", "cwd": "/tmp"}, f"[{label}] args"
