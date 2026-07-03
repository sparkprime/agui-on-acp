"""AG-UI compatible endpoint — makes this bridge a proper AG-UI server.

Any AG-UI client (CopilotKit, HttpAgent, custom clients) can POST to
this endpoint with a RunAgentInput body and receive back an SSE stream
of AG-UI events. This is the standard AG-UI server contract:

  POST /ag-ui
  Content-Type: application/json
  Accept: text/event-stream

  Body: { threadId, runId, messages, tools, state, context, forwardedProps }

  Response: text/event-stream with AG-UI events
"""

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agui.events import StateSnapshotEvent
from backend.agui.sse import event_stream

logger = logging.getLogger(__name__)

router = APIRouter()


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: dict[str, Any] = Field(default_factory=dict)


class AgUiMessage(BaseModel):
    """AG-UI message. `content` is optional because assistant messages
    that only carry tool calls (no text) legitimately omit it, per the
    AG-UI spec's AssistantMessageSchema."""

    id: str | None = None
    role: str
    content: str | None = None
    name: str | None = None
    toolCalls: list[ToolCall] | None = None
    toolCallId: str | None = None

class ResumeEntry(BaseModel):
    """AG-UI ResumeEntry (types.ts:203)."""

    interruptId: str
    status: str = "resolved"  # "resolved" | "cancelled"
    payload: Any = None


class RunAgentInput(BaseModel):
    """AG-UI standard RunAgentInput schema."""

    threadId: str | None = None
    runId: str | None = None
    state: dict[str, Any] = Field(default_factory=dict)
    messages: list[AgUiMessage] = Field(default_factory=list)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    context: list[Any] = Field(default_factory=list)
    forwardedProps: dict[str, Any] = Field(default_factory=dict)
    resume: list[ResumeEntry] = Field(default_factory=list)


@router.post("/ag-ui")
async def ag_ui_run(body: RunAgentInput, request: Request):
    """AG-UI standard run endpoint.

    Accepts RunAgentInput, creates/reuses a session, sends the prompt,
    and streams back AG-UI events over SSE. This makes the bridge
    compatible with any AG-UI client (CopilotKit, HttpAgent, etc.).

    If ``body.resume`` is non-empty, this is a resume run: the prompt task
    is already suspended at a permission interrupt, so we re-attach a new
    SSE stream and resolve the parked Future instead of starting a new
    ``prompt()``.
    """
    manager = getattr(request.app.state, "session_manager", None)
    if manager is None:
        return StreamingResponse(
            _error_stream("Session manager not initialized"),
            media_type="text/event-stream",
        )

    thread_id = body.threadId or str(uuid.uuid4())
    run_id = body.runId or str(uuid.uuid4())

    # ── Resume path ──────────────────────────────────────────────────────
    if body.resume:
        try:
            actual_run_id = await manager.resume_run(thread_id, [r.model_dump() for r in body.resume])
        except KeyError:
            return StreamingResponse(
                _error_stream(f"No active session for thread {thread_id}"),
                media_type="text/event-stream",
            )
        except ValueError as exc:
            # No pending interrupt to resume — surface as RUN_ERROR instead
            # of a hanging empty stream.
            return StreamingResponse(
                _error_stream(str(exc)),
                media_type="text/event-stream",
            )

        queue = manager.get_event_queue(thread_id, actual_run_id)
        if queue is None:
            return StreamingResponse(
                _error_stream("No event queue for resume run"),
                media_type="text/event-stream",
            )
        return _sse_response(queue, thread_id, manager)

    # ── Fresh run path ───────────────────────────────────────────────────
    active = manager._sessions.get(thread_id)
    if active is None:
        fp = body.forwardedProps
        cwd = fp.get("cwd", ".")
        title = fp.get("title", "AG-UI Session")
        resume_session_id = fp.get("resumeSessionId")
        mode = fp.get("mode")
        model = fp.get("model")
        agent_command = fp.get("agentCommand")
        try:
            active = await manager.create_task(
                task_id=thread_id,
                cwd=cwd,
                title=title,
                resume_session_id=resume_session_id,
                mode=mode,
                model=model,
                agent_command=agent_command,
            )
        except Exception as exc:
            logger.error("Failed to create session for AG-UI: %s", exc)
            return StreamingResponse(
                _error_stream(str(exc)),
                media_type="text/event-stream",
            )

    # Emit a STATE_SNAPSHOT with available modes/models so the UI can
    # populate selectors (design-v2: STATE_SNAPSHOT for modes/models).
    snapshot: dict[str, Any] = {}
    if active.modes:
        snapshot["modes"] = active.modes
    if active.models:
        snapshot["models"] = active.models
    if active.current_mode_id:
        snapshot["currentModeId"] = active.current_mode_id
    if snapshot:
        active.bridge._emit(StateSnapshotEvent(snapshot=snapshot))

    # Extract the last user message
    user_message = ""
    if body.messages:
        for msg in reversed(body.messages):
            if msg.role == "user" and msg.content:
                user_message = msg.content
                break

    if not user_message:
        return StreamingResponse(
            _error_stream("No user message provided"),
            media_type="text/event-stream",
        )

    # Start a run
    try:
        actual_run_id = await manager.start_run(
            thread_id,
            {"messages": [{"role": "user", "content": user_message}]},
        )
    except Exception as exc:
        logger.error("Failed to start run: %s", exc)
        return StreamingResponse(
            _error_stream(str(exc)),
            media_type="text/event-stream",
        )

    queue = manager.get_event_queue(thread_id, actual_run_id)
    if queue is None:
        return StreamingResponse(
            _error_stream("No event queue for run"),
            media_type="text/event-stream",
        )

    return _sse_response(queue, thread_id, manager)


def _sse_response(queue: asyncio.Queue, thread_id: str, manager: Any) -> StreamingResponse:
    """Build a StreamingResponse with the cancel-on-disconnect callback."""
    async def _on_disconnect() -> None:
        await manager.cancel_run(thread_id)

    return StreamingResponse(
        event_stream(queue, thread_id, on_cancel=_on_disconnect),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _error_stream(message: str):
    """Yield a single RUN_ERROR event."""
    import json
    import time
    error_event = {
        "type": "RUN_ERROR",
        "timestamp": time.time(),
        "message": message,
        "runId": str(uuid.uuid4()),
        "taskId": "error",
    }
    yield f"event: RUN_ERROR\ndata: {json.dumps(error_event)}\n\n"
