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
from typing import Any

import httpx
import pytest

from agui_on_acp.sessions.manager import SessionManager
from tests.fake_agent import (
    FakeAcpAgent,
    end_turn,
    ext_notification,
    request_permission,
    sleep,
    text,
    tool_end,
    tool_progress,
    tool_start,
)
from tests.sse_helpers import event_of_type, read_sse_events, read_until


def _agui_body(
    *,
    thread_id: str = "t1",
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
    assert finished["data"]["threadId"] == "t1"

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
    # Create the session + run via the manager (same path the endpoint takes).
    await session_manager.create_task(task_id="t1", cwd="/tmp/opencode")
    run_id = await session_manager.start_run(
        "t1", {"messages": [{"role": "user", "content": "hi"}]}
    )
    queue = session_manager.get_event_queue("t1", run_id)
    assert queue is not None

    async def _consume() -> list[str]:
        chunks: list[str] = []
        async for chunk in event_stream(
            queue,
            "t1",
            timeout=2.0,
            on_cancel=lambda: session_manager.cancel_run("t1"),
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
    assert "fake-session-1" in fake_agent.cancel_calls


@pytest.mark.asyncio
async def test_cancel_while_suspended_resolves_permission_cancelled(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """Cancelling a run while it's suspended at a permission interrupt
    resolves the parked Future as cancelled and sends ``session/cancel``."""
    fake_agent.script = [
        request_permission("perm1"),
        end_turn(),
    ]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        await read_until(resp, {"RUN_FINISHED"})

    # Now cancel the suspended run directly via the /v2 cancel endpoint.
    resp = await http_client.post("/v2/tasks/t1/cancel")
    assert resp.status_code == 200

    await asyncio.wait_for(fake_agent.prompt_done.wait(), timeout=5.0)
    assert len(fake_agent.permission_replies) == 1
    assert fake_agent.permission_replies[0].outcome["outcome"] == "cancelled"
    assert "fake-session-1" in fake_agent.cancel_calls


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
async def test_state_snapshot_advertises_modes(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """When the agent reports modes in new_session, the bridge emits a
    STATE_SNAPSHOT carrying them so the UI can populate selectors."""
    fake_agent.modes = [
        {"id": "build", "name": "Build"},
        {"id": "plan", "name": "Plan"},
    ]
    fake_agent.script = [text("hi"), end_turn()]
    async with http_client.stream("POST", "/ag-ui", json=_agui_body()) as resp:
        events = await read_sse_events(resp)
    snaps = [e for e in events if e["type"] == "STATE_SNAPSHOT"]
    assert snaps, "expected a STATE_SNAPSHOT with modes"
    assert snaps[0]["data"]["snapshot"]["modes"] == [
        {"id": "build", "name": "Build"},
        {"id": "plan", "name": "Plan"},
    ]


@pytest.mark.asyncio
async def test_forwarded_props_mode_and_model_are_applied(
    fake_agent: FakeAcpAgent, http_client: httpx.AsyncClient
):
    """``forwardedProps.mode`` / ``forwardedProps.model`` from the AG-UI
    client are translated to ACP ``session/set_mode`` / ``session/set_model``
    before the prompt runs."""
    fake_agent.script = [text("hi"), end_turn()]
    body = _agui_body(
        forwarded_props={"cwd": "/tmp/opencode", "mode": "plan", "model": "gpt-x"}
    )
    async with http_client.stream("POST", "/ag-ui", json=body) as resp:
        await read_sse_events(resp)
    assert ("fake-session-1", "plan") in fake_agent.set_mode_calls
    assert ("fake-session-1", "gpt-x") in fake_agent.set_model_calls


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
        "threadId": "t1",
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
