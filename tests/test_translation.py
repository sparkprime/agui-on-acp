"""Integration tests for the AG-UI on ACP translation layer.

These tests drive the bridge end-to-end through both real protocol
surfaces:

  - AG-UI input: POST /ag-ui with RunAgentInput, consume SSE event stream.
  - ACP output: a FakeAcpAgent speaking real ACP JSON-RPC over an
    in-memory asyncio stream pair (only the OS subprocess is replaced).

Each test programs the fake agent with a script of session_update
notifications / request_permission calls, then asserts on the AG-UI events
the bridge emits on the SSE stream.

Scope: every code path that contributes to "this is an ACP/AG-UI translator
that adds no new features" — text streaming, tool-call lifecycle, the
interrupt/resume permission flow (the core impedance-mismatch fix from
design-v2), cancel, disconnect, permission TTL expiry, and error paths.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import httpx
import pytest

from agui_on_acp.sessions.manager import SessionManager
from tests.fake_agent import (
    FakeAcpAgent,
    config_option_update,
    elicitation,
    end_turn,
    ext_notification,
    plan,
    plan_removed,
    request_permission,
    session_info,
    sleep,
    text,
    thought,
    tool_end,
    tool_progress,
    tool_start,
    usage,
)
from tests.sse_helpers import event_of_type, read_sse_events, read_until
from tests.conftest import make_stack, teardown_stack


def _agui_body(
    *,
    thread_id: str = "fake-session-1",
    content: str = "hello",
    forwarded_props: dict[str, Any] | None = None,
    resume: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "threadId": thread_id,
        "runId": "r1",
        "messages": [{"role": "user", "id": "u1", "content": content}],
        "forwardedProps": forwarded_props or {"cwd": "/tmp/opencode"},
    }
    if resume is not None:
        body["resume"] = resume
    return body


# ─────────────────────────────────────────────────────────────────────────────
# Basic text turn
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_text_turn_streams_start_content_end_then_finished(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A simple agent text turn maps to TEXT_MESSAGE_START / CONTENT / END +
    RUN_FINISHED with no interrupt outcome."""
    fake_agent.script = [
        text("hello "),
        text("world"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        assert resp.status_code == 200
        events = await read_sse_events(resp)

    types = [e["type"] for e in events]
    assert types[0] == "RUN_STARTED"
    assert "TEXT_MESSAGE_START" in types
    start_idx = types.index("TEXT_MESSAGE_START")
    end_idx = types.index("TEXT_MESSAGE_END")
    # Every CONTENT falls between START and END.
    assert start_idx < end_idx
    assert all(
        types[i] == "TEXT_MESSAGE_CONTENT" for i in range(start_idx + 1, end_idx)
    )
    assert types[-1] == "RUN_FINISHED"

    finished = event_of_type(events, "RUN_FINISHED")
    assert finished["data"].get("outcome") is None
    assert finished["data"]["threadId"] == "fake-session-1"

    content = "".join(
        e["data"]["delta"] for e in events if e["type"] == "TEXT_MESSAGE_CONTENT"
    )
    assert content == "hello world"

    # The bridge forwarded the prompt to the agent as a text block.
    assert len(fake_agent.prompt_calls) == 1
    pc = fake_agent.prompt_calls[0]
    assert pc.session_id == "fake-session-1"
    assert len(pc.prompt) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Tool call lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_call_emits_start_args_end_and_result(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """ToolCallStart/Progress/completed maps to TOOL_CALL_START, TOOL_CALL_ARGS,
    TOOL_CALL_END, and the TOOL_CALL_RESULT event CopilotKit needs to flip
    the renderer to complete."""
    fake_agent.script = [
        tool_start("tc1", title="read file", kind="read", raw_input={"path": "/a"}),
        tool_progress("tc1", status="in_progress"),
        tool_end("tc1", status="completed", raw_output={"output": "file-contents"}),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)

    types = [e["type"] for e in events]
    assert "TOOL_CALL_START" in types
    assert types.index("TOOL_CALL_START") < types.index("TOOL_CALL_ARGS")
    assert "TOOL_CALL_END" in types
    assert "TOOL_CALL_RESULT" in types

    start = event_of_type(events, "TOOL_CALL_START")
    assert start["data"]["toolCallId"] == "tc1"
    assert start["data"]["toolCallName"] == "read file"

    result = event_of_type(events, "TOOL_CALL_RESULT")
    assert result["data"]["toolCallId"] == "tc1"
    assert result["data"]["content"] == "file-contents"
    assert result["data"]["role"] == "tool"


# ─────────────────────────────────────────────────────────────────────────────
# Interrupt / resume — the core impedance-mismatch fix
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permission_request_interrupts_run_then_resume_resolves(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A ``request_permission`` mid-turn emits a
    ``RUN_FINISHED{outcome:interrupt}``, parks the prompt task, and a
    subsequent resume run re-attaches the stream and resolves the
    permission so the prompt continues."""
    fake_agent.script = [
        text("before-approval"),
        request_permission("perm1", title="run bash"),
        text("after-approval"),
        end_turn(),
    ]

    # ── Run 1: should end with an interrupt ──────────────────────────────
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        run1 = await read_until(resp, {"RUN_FINISHED"})

    finished = event_of_type(run1, "RUN_FINISHED")
    outcome = finished["data"]["outcome"]
    assert outcome["type"] == "interrupt"
    assert len(outcome["interrupts"]) == 1
    interrupt = outcome["interrupts"][0]
    assert interrupt["id"] == "perm1"
    assert interrupt["toolCallId"] == "perm1"
    assert interrupt["reason"] == "tool_call"
    assert interrupt["expiresAt"] is not None

    # "before-approval" text was streamed, "after-approval" was not yet.
    r1_text = "".join(
        e["data"]["delta"] for e in run1 if e["type"] == "TEXT_MESSAGE_CONTENT"
    )
    assert r1_text == "before-approval"

    # ── Run 2: resume with "resolved" → prompt continues to end of turn ──
    resume_body = _agui_body(
        resume=[{"interruptId": "perm1", "status": "resolved", "payload": "once"}]
    )
    async with http_client.stream("POST", "/ag-ui", json=resume_body) as resp:
        run2 = await read_sse_events(resp)

    assert run2[0]["type"] == "RUN_STARTED"
    r2_text = "".join(
        e["data"]["delta"] for e in run2 if e["type"] == "TEXT_MESSAGE_CONTENT"
    )
    assert r2_text == "after-approval"
    assert run2[-1]["type"] == "RUN_FINISHED"
    assert run2[-1]["data"].get("outcome") is None

    # The bridge drove the ACP prompt to completion across both runs.
    assert len(fake_agent.prompt_calls) == 1
    assert len(fake_agent.permission_replies) == 1
    reply = fake_agent.permission_replies[0]
    assert reply.tool_call_id == "perm1"
    assert reply.outcome["outcome"] == "selected"
    assert reply.outcome["optionId"] == "once"


@pytest.mark.asyncio
async def test_permission_resume_cancelled_replies_cancelled_to_acp(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A resume with status "cancelled" resolves the ACP permission as
    ``cancelled`` (ACP's DeniedOutcome), not ``selected``."""
    fake_agent.script = [
        request_permission("perm1"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_until(resp, {"RUN_FINISHED"})

    resume_body = _agui_body(resume=[{"interruptId": "perm1", "status": "cancelled"}])
    async with http_client.stream("POST", "/ag-ui", json=resume_body) as resp:
        await read_sse_events(resp)

    assert len(fake_agent.permission_replies) == 1
    assert fake_agent.permission_replies[0].outcome["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_resume_with_no_pending_interrupt_yields_run_error(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A resume run for a session with no parked permission surfaces a
    RUN_ERROR rather than hanging on an empty stream."""
    # First do a normal turn so the session exists, then send a resume.
    fake_agent.script = [text("hi"), end_turn()]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_sse_events(resp)

    resume_body = _agui_body(resume=[{"interruptId": "nope", "status": "resolved"}])
    async with http_client.stream("POST", "/ag-ui", json=resume_body) as resp:
        events = await read_sse_events(resp)
    assert any(e["type"] == "RUN_ERROR" for e in events)


@pytest.mark.asyncio
async def test_resume_for_unknown_session_yields_run_error(
    http_client: httpx.AsyncClient,
):
    """A resume for a threadId with no active session surfaces RUN_ERROR."""
    body = _agui_body(
        thread_id="never-existed",
        resume=[{"interruptId": "x", "status": "resolved"}],
    )
    async with http_client.stream("POST", "/ag-ui", json=body) as resp:
        events = await read_sse_events(resp)
    assert any(e["type"] == "RUN_ERROR" for e in events)


# ─────────────────────────────────────────────────────────────────────────────
# Permission TTL expiry (resume never arrives)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_permission_future_expires_when_no_resume_arrives(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """If no resume ever arrives, the parked permission Future expires
    (TTL) and resolves with ``cancelled`` so the prompt task unwinds
    instead of hanging forever (leaking the ACP subprocess)."""
    fake_agent.script = [
        request_permission("perm1"),
        end_turn(),
    ]
    # Consume run 1 (the interrupt) and then walk away without resuming.
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_until(resp, {"RUN_FINISHED"})

    # The prompt task is parked. Wait long enough for the (shortened) TTL.
    await asyncio.wait_for(fake_agent.prompt_done.wait(), timeout=5.0)

    assert len(fake_agent.permission_replies) == 1
    assert fake_agent.permission_replies[0].outcome["outcome"] == "cancelled"


# ─────────────────────────────────────────────────────────────────────────────
# Cancel / disconnect
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_disconnect_triggers_acp_cancel(
    fake_agent: FakeAcpAgent,
    session_manager: SessionManager,
    precreated_session_id: str,
    http_client: httpx.AsyncClient,
):
    """When the AG-UI client disconnects mid-run (CancelledError on the SSE
    generator), the bridge calls ``session/cancel`` on the ACP agent and
    resolves any parked permission futures as cancelled.

    A real TCP disconnect cancels the ``StreamingResponse`` body iterator
    task, which raises ``CancelledError`` inside ``event_stream``. httpx's
    ``ASGITransport`` doesn't simulate that cancellation on early stream
    close, so we drive it directly: start the run, then cancel the task
    consuming the SSE ``event_stream`` — the exact event the ASGI server
    delivers on a real socket close."""
    from agui_on_acp.agui.sse import event_stream

    fake_agent.script = [
        text("streaming..."),
        sleep(10.0),  # hold the turn open so we can disconnect mid-stream
        end_turn(),
    ]
    sid = precreated_session_id
    # Start a run on the pre-created session via the manager.
    run_id = await session_manager.start_run(
        sid, {"messages": [{"role": "user", "content": "hi"}]}
    )
    queue = session_manager.get_event_queue(sid, run_id)
    assert queue is not None

    async def _consume() -> list[str]:
        chunks: list[str] = []
        async for chunk in event_stream(
            queue,
            sid,
            timeout=2.0,
            on_cancel=lambda: session_manager.cancel_run(sid),
        ):
            chunks.append(chunk)
        return chunks

    task = asyncio.create_task(_consume())
    # Let it receive RUN_STARTED + the text delta, then "disconnect".
    await asyncio.sleep(0.3)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    # The on_cancel callback ran cancel_run → session/cancel to the agent.
    await asyncio.sleep(0.2)
    assert sid in fake_agent.cancel_calls


@pytest.mark.asyncio
async def test_cancel_while_suspended_resolves_permission_cancelled(
    fake_agent: FakeAcpAgent,
    session_manager: SessionManager,
    precreated_session_id: str,
    http_client: httpx.AsyncClient,
):
    """Cancelling a run while it's suspended at a permission interrupt
    resolves the parked Future as cancelled and sends ``session/cancel``."""
    fake_agent.script = [
        request_permission("perm1"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_until(resp, {"RUN_FINISHED"})

    # Cancel the suspended run directly via the manager (the AG-UI surface
    # has no separate cancel endpoint; clients either resume with
    # status="cancelled" or let the permission TTL expire).
    await session_manager.cancel_run(precreated_session_id)

    await asyncio.wait_for(fake_agent.prompt_done.wait(), timeout=5.0)
    assert len(fake_agent.permission_replies) == 1
    assert fake_agent.permission_replies[0].outcome["outcome"] == "cancelled"
    assert precreated_session_id in fake_agent.cancel_calls


# ─────────────────────────────────────────────────────────────────────────────
# Extension notifications → CUSTOM events
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_kiro_dev_notification_becomes_custom_event(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A ``_kiro.dev/*`` ext notification mid-turn is translated to an
    AG-UI CUSTOM event with a mapped name."""
    fake_agent.script = [
        ext_notification("_kiro.dev/metadata", foo="bar"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    customs = [e for e in events if e["type"] == "CUSTOM"]
    assert len(customs) == 1
    assert customs[0]["data"]["name"] == "agent:metadata"
    assert customs[0]["data"]["value"] == {"foo": "bar"}


@pytest.mark.asyncio
async def test_pre_run_notification_is_buffered_then_flushed(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """An ext notification that arrives before any run starts is buffered
    and flushed as a CUSTOM event when the first run begins (so session-init
    notifications aren't lost)."""
    # Drive the notification before the run by poking the agent directly:
    # the FakeAcpAgent only runs its script during prompt(), so to test the
    # buffer path we send a notification as a preamble step.
    fake_agent.script = [
        ext_notification("_kiro.dev/mcp/server_initialized", id="srv1"),
        text("hi"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    customs = [e for e in events if e["type"] == "CUSTOM"]
    assert any(c["data"]["name"] == "agent:mcp_initialized" for c in customs)


# ─────────────────────────────────────────────────────────────────────────────
# Modes / models snapshot
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_snapshot_advertises_modes():
    """When the agent reports modes in new_session, the bridge emits a
    STATE_SNAPSHOT carrying them so the UI can populate selectors."""
    fake, manager, client = await make_stack()
    try:
        fake.modes = [
            {"id": "build", "name": "Build"},
            {"id": "plan", "name": "Plan"},
        ]
        fake.script = [text("hi"), end_turn()]
        active = await manager.create_session(cwd="/tmp/opencode")
        body = _agui_body(thread_id=active.session_id)
        async with client.stream("POST", "/ag-ui", json=body) as resp:
            events = await read_sse_events(resp)
        snaps = [e for e in events if e["type"] == "STATE_SNAPSHOT"]
        assert snaps, "expected a STATE_SNAPSHOT with modes"
        assert snaps[0]["data"]["snapshot"]["modes"] == [
            {"id": "build", "name": "Build"},
            {"id": "plan", "name": "Plan"},
        ]
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_create_session_applies_mode_and_model():
    """``POST /ag-ui/sessions`` with ``mode`` / ``model`` translates to ACP
    ``session/set_mode`` / ``session/set_model`` at create time (the prompt
    path no longer applies them — moved to the Create endpoint)."""
    fake, manager, client = await make_stack()
    try:
        fake.script = [text("hi"), end_turn()]
        resp = await client.post(
            "/ag-ui/sessions",
            json={"cwd": "/tmp/opencode", "mode": "plan", "model": "gpt-x"},
        )
        assert resp.status_code == 201
        sid = resp.json()["sessionId"]
        assert (sid, "plan") in fake.set_mode_calls
        assert (sid, "gpt-x") in fake.set_model_calls
    finally:
        await teardown_stack(fake, manager, client)


# ─────────────────────────────────────────────────────────────────────────────
# Error paths
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_user_message_yields_run_error(
    http_client: httpx.AsyncClient,
):
    """A RunAgentInput with no user message surfaces a RUN_ERROR stream
    instead of starting a turn."""
    body = {
        "threadId": "fake-session-1",
        "runId": "r1",
        "messages": [{"role": "assistant", "content": "no user here"}],
        "forwardedProps": {"cwd": "/tmp/opencode"},
    }
    async with http_client.stream("POST", "/ag-ui", json=body) as resp:
        events = await read_sse_events(resp)
    assert any(e["type"] == "RUN_ERROR" for e in events)


@pytest.mark.asyncio
async def test_prompt_exception_becomes_run_error(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """If the agent's prompt raises, the bridge emits RUN_ERROR (not a
    hanging stream)."""
    # Set the exception hook before the run starts; prompt() checks it at
    # call time (the router captured the bound method at attach()).
    fake_agent.prompt_exception = RuntimeError("agent exploded")
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    assert any(e["type"] == "RUN_ERROR" for e in events)


# ─────────────────────────────────────────────────────────────────────────────
# ACP 0.11 — config options read path (P0)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_snapshot_advertises_config_options():
    """When the agent reports ``configOptions`` in new_session, the bridge
    emits them in the post-start STATE_SNAPSHOT so the UI can populate the
    config/model selector."""
    fake, manager, client = await make_stack()
    try:
        fake.config_options = [
            {
                "id": "model",
                "name": "Model",
                "type": "select",
                "currentValue": "gpt-x",
                "options": [
                    {"value": "gpt-x", "name": "GPT X"},
                    {"value": "claude-y", "name": "Claude Y"},
                ],
            },
            {
                "id": "verbose",
                "name": "Verbose",
                "type": "boolean",
                "currentValue": False,
            },
        ]
        fake.script = [text("hi"), end_turn()]
        active = await manager.create_session(cwd="/tmp/opencode")
        body = _agui_body(thread_id=active.session_id)
        async with client.stream("POST", "/ag-ui", json=body) as resp:
            events = await read_sse_events(resp)
        snaps = [e for e in events if e["type"] == "STATE_SNAPSHOT"]
        assert snaps, "expected a STATE_SNAPSHOT with configOptions"
        opts = snaps[0]["data"]["snapshot"]["configOptions"]
        by_id = {o["id"]: o for o in opts}
        assert by_id["model"]["type"] == "select"
        assert by_id["model"]["currentValue"] == "gpt-x"
        assert by_id["model"]["options"] == [
            {"value": "gpt-x", "name": "GPT X"},
            {"value": "claude-y", "name": "Claude Y"},
        ]
        assert by_id["verbose"]["type"] == "boolean"
        assert by_id["verbose"]["currentValue"] is False
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_config_option_update_notification_emits_state_snapshot(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A mid-turn ``ConfigOptionUpdate`` notification is surfaced as a fresh
    STATE_SNAPSHOT carrying the updated ``configOptions`` (replace, not
    patch)."""
    fake_agent.script = [
        config_option_update(
            [
                {
                    "id": "model",
                    "name": "Model",
                    "type": "select",
                    "currentValue": "claude-y",
                    "options": [
                        {"value": "claude-y", "name": "Claude Y"},
                        {"value": "gpt-x", "name": "GPT X"},
                    ],
                }
            ]
        ),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    snaps = [e for e in events if e["type"] == "STATE_SNAPSHOT"]
    model_snap = [
        s
        for s in snaps
        if "configOptions" in s["data"]["snapshot"]
        and s["data"]["snapshot"]["configOptions"][0]["id"] == "model"
    ]
    assert model_snap, "expected a STATE_SNAPSHOT from the ConfigOptionUpdate"
    assert model_snap[-1]["data"]["snapshot"]["configOptions"][0]["currentValue"] == (
        "claude-y"
    )


@pytest.mark.asyncio
async def test_create_session_applies_config_options():
    """``POST /ag-ui/sessions`` with ``configOptions`` applies each via
    ``session/set_config_option`` at create time (the prompt path no longer
    applies them — moved to the Create endpoint)."""
    fake, manager, client = await make_stack()
    try:
        fake.script = [text("hi"), end_turn()]
        resp = await client.post(
            "/ag-ui/sessions",
            json={
                "cwd": "/tmp/opencode",
                "model": "gpt-x",
                "configOptions": {"theme": "dark", "verbose": True},
            },
        )
        assert resp.status_code == 201
        sid = resp.json()["sessionId"]
        # The legacy "model" field was applied via set_model (config_id="model").
        assert (sid, "gpt-x") in fake.set_model_calls
        # The generic config options were applied via set_config_option. The
        # "model" entry in configOptions is skipped (handled above), but
        # "theme" and "verbose" are forwarded.
        applied_ids = {cid for (_sid, cid, _val) in fake.set_config_option_calls}
        assert "theme" in applied_ids
        assert "verbose" in applied_ids
        assert "model" in applied_ids  # via the legacy field
        # No duplicate "model" application from the configOptions dict.
        model_calls = [
            (cid, val)
            for (_sid, cid, val) in fake.set_config_option_calls
            if cid == "model"
        ]
        assert model_calls == [("model", "gpt-x")]
    finally:
        await teardown_stack(fake, manager, client)


# ─────────────────────────────────────────────────────────────────────────────
# ACP 0.11 — usage / session info / plans / thought (P1, P4)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_update_becomes_custom_agent_usage(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    fake_agent.script = [
        usage(used=4200, size=200000, cost={"amount": 0.03, "currency": "USD"}),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    customs = [
        e
        for e in events
        if e["type"] == "CUSTOM" and e["data"]["name"] == "agent:usage"
    ]
    assert customs, "expected an agent:usage CUSTOM event"
    val = customs[-1]["data"]["value"]
    assert val["used"] == 4200
    assert val["size"] == 200000
    assert val["cost"]["amount"] == 0.03
    assert val["cost"]["currency"] == "USD"


@pytest.mark.asyncio
async def test_session_info_update_becomes_custom(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    fake_agent.script = [
        session_info(title="My conversation", updated_at="2026-01-01T00:00:00Z"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    customs = [
        e
        for e in events
        if e["type"] == "CUSTOM" and e["data"]["name"] == "agent:session_info"
    ]
    assert customs, "expected an agent:session_info CUSTOM event"
    assert customs[-1]["data"]["value"]["title"] == "My conversation"


@pytest.mark.asyncio
async def test_plan_update_and_removed_become_custom(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    fake_agent.script = [
        plan(
            entries=[
                {"content": "do thing", "priority": "high", "status": "pending"},
                {"content": "do other", "priority": "low", "status": "completed"},
            ]
        ),
        plan_removed("plan-1"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    names = [e["data"]["name"] for e in events if e["type"] == "CUSTOM"]
    assert "agent:plan" in names
    assert "agent:plan_removed" in names
    customs = [e for e in events if e["type"] == "CUSTOM"]
    plan_evt = next(e for e in customs if e["data"]["name"] == "agent:plan")
    assert len(plan_evt["data"]["value"]["entries"]) == 2
    removed_evt = next(e for e in customs if e["data"]["name"] == "agent:plan_removed")
    assert removed_evt["data"]["value"]["id"] == "plan-1"


@pytest.mark.asyncio
async def test_agent_thought_chunk_becomes_custom_agent_thought(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    fake_agent.script = [thought("reasoning about the problem"), end_turn()]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    customs = [
        e
        for e in events
        if e["type"] == "CUSTOM" and e["data"]["name"] == "agent:thought"
    ]
    assert customs, "expected an agent:thought CUSTOM event"
    assert customs[-1]["data"]["value"]["delta"] == "reasoning about the problem"


# ─────────────────────────────────────────────────────────────────────────────
# ACP 0.11 — elicitation as an interrupt (P2)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_elicitation_interrupts_run_then_resume_accepts(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A ``create_elicitation`` mid-turn emits a
    ``RUN_FINISHED{outcome:interrupt}`` with ``reason="elicitation"`` and a
    ``responseSchema``, parks the prompt task, and a subsequent resume with
    an accepted payload resolves the elicitation with the form values."""
    fake_agent.script = [
        text("before-elicitation"),
        elicitation(
            message="What is your name?",
            mode_kind="form_session",
            requested_schema={
                "properties": {"name": {"type": "string", "title": "Name"}}
            },
        ),
        text("after-elicitation"),
        end_turn(),
    ]

    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        run1 = await read_until(resp, {"RUN_FINISHED"})

    finished = event_of_type(run1, "RUN_FINISHED")
    outcome = finished["data"]["outcome"]
    assert outcome["type"] == "interrupt"
    interrupt = outcome["interrupts"][0]
    assert interrupt["reason"] == "elicitation"
    assert interrupt["message"] == "What is your name?"
    assert interrupt["responseSchema"] is not None
    elicitation_id = interrupt["id"]

    r1_text = "".join(
        e["data"]["delta"] for e in run1 if e["type"] == "TEXT_MESSAGE_CONTENT"
    )
    assert r1_text == "before-elicitation"

    resume_body = _agui_body(
        resume=[
            {
                "interruptId": elicitation_id,
                "status": "resolved",
                "payload": {"status": "accepted", "values": {"name": "Ada"}},
            }
        ]
    )
    async with http_client.stream("POST", "/ag-ui", json=resume_body) as resp:
        run2 = await read_sse_events(resp)

    r2_text = "".join(
        e["data"]["delta"] for e in run2 if e["type"] == "TEXT_MESSAGE_CONTENT"
    )
    assert r2_text == "after-elicitation"

    assert len(fake_agent.elicitation_replies) == 1
    reply = fake_agent.elicitation_replies[0]
    assert reply.action == "accept"
    assert reply.content == {"name": "Ada"}


@pytest.mark.asyncio
async def test_elicitation_resume_declined(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A resume with a declined payload resolves the elicitation as
    ``DeclineElicitationResponse``."""
    fake_agent.script = [
        elicitation(message="optional question"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        run1 = await read_until(resp, {"RUN_FINISHED"})
    elicitation_id = event_of_type(run1, "RUN_FINISHED")["data"]["outcome"][
        "interrupts"
    ][0]["id"]

    resume_body = _agui_body(
        resume=[
            {
                "interruptId": elicitation_id,
                "status": "resolved",
                "payload": {"status": "declined"},
            }
        ]
    )
    async with http_client.stream("POST", "/ag-ui", json=resume_body) as resp:
        await read_sse_events(resp)

    assert len(fake_agent.elicitation_replies) == 1
    assert fake_agent.elicitation_replies[0].action == "decline"


@pytest.mark.asyncio
async def test_elicitation_resume_cancelled(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """A resume with ``status="cancelled"`` resolves the elicitation as
    ``CancelElicitationResponse``."""
    fake_agent.script = [
        elicitation(),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        run1 = await read_until(resp, {"RUN_FINISHED"})
    elicitation_id = event_of_type(run1, "RUN_FINISHED")["data"]["outcome"][
        "interrupts"
    ][0]["id"]

    resume_body = _agui_body(
        resume=[{"interruptId": elicitation_id, "status": "cancelled"}]
    )
    async with http_client.stream("POST", "/ag-ui", json=resume_body) as resp:
        await read_sse_events(resp)

    assert len(fake_agent.elicitation_replies) == 1
    assert fake_agent.elicitation_replies[0].action == "cancel"


@pytest.mark.asyncio
async def test_cancel_while_suspended_at_elicitation_resolves_cancelled(
    fake_agent: FakeAcpAgent,
    session_manager: SessionManager,
    precreated_session_id: str,
    http_client: httpx.AsyncClient,
):
    """Cancelling a run while it's suspended at an elicitation resolves the
    parked Future as cancelled."""
    fake_agent.script = [
        elicitation(),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_until(resp, {"RUN_FINISHED"})

    await session_manager.cancel_run(precreated_session_id)
    await asyncio.wait_for(fake_agent.prompt_done.wait(), timeout=5.0)
    assert len(fake_agent.elicitation_replies) == 1
    assert fake_agent.elicitation_replies[0].action == "cancel"


@pytest.mark.asyncio
async def test_elicitation_future_expires_when_no_resume_arrives(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """If no resume ever arrives, the parked elicitation Future expires (TTL)
    and resolves with ``cancel`` so the prompt task unwinds."""
    fake_agent.script = [
        elicitation(),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_until(resp, {"RUN_FINISHED"})
    await asyncio.wait_for(fake_agent.prompt_done.wait(), timeout=5.0)
    assert len(fake_agent.elicitation_replies) == 1
    assert fake_agent.elicitation_replies[0].action == "cancel"


# ─────────────────────────────────────────────────────────────────────────────
# Mid-session config change endpoint (P3)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_ag_ui_config_applies_config_options(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """``POST /ag-ui/config`` applies each supplied config option via
    ``session/set_config_option`` without starting a new run."""
    # Establish a session first.
    fake_agent.script = [text("hi"), end_turn()]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_sse_events(resp)

    resp = await http_client.post(
        "/ag-ui/config",
        json={"threadId": "fake-session-1", "configOptions": {"model": "claude-y"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["applied"] == ["model"]
    assert ("fake-session-1", "claude-y") in fake_agent.set_model_calls


@pytest.mark.asyncio
async def test_post_ag_ui_config_unknown_session(
    http_client: httpx.AsyncClient,
):
    resp = await http_client.post(
        "/ag-ui/config",
        json={"threadId": "nope", "configOptions": {"model": "x"}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


# ─────────────────────────────────────────────────────────────────────────────
# MCP servers via forwardedProps (P5)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_session_passes_mcp_servers():
    """``POST /ag-ui/sessions`` with ``mcpServers`` plumbs them through to
    the ACP ``session/new`` call."""
    fake, manager, client = await make_stack()
    try:
        fake.script = [text("hi"), end_turn()]
        resp = await client.post(
            "/ag-ui/sessions",
            json={
                "cwd": "/tmp/opencode",
                "mcpServers": {
                    "github": {"type": "http", "url": "https://example/mcp"},
                },
            },
        )
        assert resp.status_code == 201
        assert len(fake.new_session_calls) == 1
        mcp = fake.new_session_calls[0]["mcp_servers"]
        assert mcp, "expected mcp_servers to be forwarded to session/new"
        servers = cast(list[Any], mcp)
        found_http = False
        for s in servers:
            if isinstance(s, dict):
                s_dict = cast(dict[str, Any], s)
                stype: Any = s_dict.get("type")
            else:
                stype = getattr(s, "type", None)
            if stype == "http":
                found_http = True
                break
        assert found_http, "expected an http MCP server in session/new"
    finally:
        await teardown_stack(fake, manager, client)
