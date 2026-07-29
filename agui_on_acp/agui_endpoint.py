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

from agui_on_acp.agui.events import AguiEvent, StateSnapshotEvent
from agui_on_acp.agui.sse import event_stream
from agui_on_acp.config import is_cwd_allowed
from agui_on_acp.sessions.manager import (
    ResumeUnsupportedError,
    SessionResumeFailedError,
)

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
    state: dict[str, Any] = Field(default_factory=dict[str, Any])
    messages: list[AgUiMessage] = Field(default_factory=list[AgUiMessage])
    tools: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]])
    context: list[Any] = Field(default_factory=list[Any])
    forwardedProps: dict[str, Any] = Field(default_factory=dict[str, Any])
    resume: list[ResumeEntry] = Field(default_factory=list[ResumeEntry])


@router.post("/ag-ui")
async def ag_ui_run(body: RunAgentInput, request: Request):
    """AG-UI standard run endpoint.

    Attach-only: the caller MUST have created a session first via
    ``POST /ag-ui/sessions`` (or resumed an existing one). This endpoint
    never creates a session inline and never falls back to ``session/new``
    or ``session/load`` — ``attach_for_prompt`` reuses a live
    ``ActiveSession`` when one exists for ``threadId`` and otherwise calls
    ``session/resume`` (raising a hard error if resume is unsupported or
    the id is dead, rather than minting a fresh session).

    If ``body.resume`` is non-empty, this is a resume run: the prompt task
    is already suspended at a permission interrupt, so we re-attach a new
    SSE stream and resolve the parked Future instead of starting a new
    ``prompt()``.
    """
    manager = getattr(request.app.state, "session_manager", None)
    if manager is None:
        return _error_stream("Session manager not initialized")

    thread_id = body.threadId
    if not thread_id:
        return _error_stream(
            "threadId is required — create a session first via POST /ag-ui/sessions"
        )

    # ── Resume path ──────────────────────────────────────────────────────
    if body.resume:
        try:
            actual_run_id = await manager.resume_run(
                thread_id, [r.model_dump() for r in body.resume]
            )
        except KeyError:
            return _error_stream(
                f"No active session for thread {thread_id} — that action expired, please try again"
            )
        except ValueError as exc:
            # No pending interrupt to resume — surface as RUN_ERROR instead
            # of a hanging empty stream.
            return _error_stream(str(exc))

        queue = manager.get_event_queue(thread_id, actual_run_id)
        if queue is None:
            return _error_stream("No event queue for resume run")
        return _sse_response(queue, thread_id, manager)

    # ── Fresh prompt on an existing/resumed session ─────────────────────────
    fp = body.forwardedProps
    cwd = fp.get("cwd")
    if not cwd or not is_cwd_allowed(cwd):
        return _error_stream("cwd missing or not allowed")

    try:
        active = await manager.attach_for_prompt(thread_id, cwd, fp.get("mcpServers"))
    except ResumeUnsupportedError:
        return _error_stream(
            f"No active session for thread {thread_id} and this agent does not support session/resume",
            status_code=409,
        )
    except SessionResumeFailedError:
        return _error_stream(
            f"Could not resume session {thread_id} — the conversation may have expired",
            status_code=404,
        )

    # Extract the last user message
    user_message = ""
    if body.messages:
        for msg in reversed(body.messages):
            if msg.role == "user" and msg.content:
                user_message = msg.content
                break

    if not user_message:
        return _error_stream("No user message provided")

    # Start a run
    try:
        actual_run_id = await manager.start_run(
            thread_id,
            {"messages": [{"role": "user", "content": user_message}]},
        )
    except Exception as exc:
        logger.error("Failed to start run: %s", exc)
        return _error_stream(str(exc))

    # Emit a STATE_SNAPSHOT with available modes/models AFTER start_run has
    # attached the bridge to the run's queue — emitting it before
    # start_run (the previous placement) dropped it because the bridge's
    # _queue was still None.
    snapshot: dict[str, Any] = {}
    if active.modes:
        snapshot["modes"] = active.modes
    if active.models:
        snapshot["models"] = active.models
    if active.config_options:
        snapshot["configOptions"] = active.config_options
    if active.current_mode_id:
        snapshot["currentModeId"] = active.current_mode_id
    if snapshot:
        active.bridge._emit(StateSnapshotEvent(snapshot=snapshot))

    queue = manager.get_event_queue(thread_id, actual_run_id)
    if queue is None:
        return _error_stream("No event queue for run")

    return _sse_response(queue, thread_id, manager)


class ConfigUpdateRequest(BaseModel):
    """Body for ``POST /ag-ui/config`` — a mid-session config change."""

    threadId: str
    configOptions: dict[str, Any] = Field(default_factory=dict[str, Any])


class ConfigUpdateResponse(BaseModel):
    ok: bool = True
    applied: list[str] = Field(default_factory=list[str])


@router.post("/ag-ui/config")
async def ag_ui_set_config(body: ConfigUpdateRequest, request: Request):
    """Bridge extension: apply mid-session config options without starting a
    new AG-UI run.

    AG-UI's ``POST /ag-ui`` is always either a fresh run or a resume; there is
    no "change config" request type. This endpoint fills that gap by calling
    ``session/set_config_option`` (ACP 0.11) for each supplied option. Not
    part of the AG-UI standard — clients must opt in.
    """
    manager = getattr(request.app.state, "session_manager", None)
    if manager is None:
        return {"ok": False, "error": "Session manager not initialized"}
    applied: list[str] = []
    for config_id, value in body.configOptions.items():
        try:
            await manager.set_config_option(body.threadId, config_id, value)
            applied.append(config_id)
        except KeyError:
            return {
                "ok": False,
                "error": f"No active session for thread {body.threadId}",
            }
        except Exception as exc:
            logger.warning("set_config_option %s failed: %s", config_id, exc)
    return ConfigUpdateResponse(applied=applied)


def _sse_response(
    queue: asyncio.Queue[AguiEvent], thread_id: str, manager: Any
) -> StreamingResponse:
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


def _error_stream(message: str, *, status_code: int = 200):
    """Yield a single ``RUN_ERROR`` event (optionally with a non-200 status)."""
    import json
    import time

    error_event = {
        "type": "RUN_ERROR",
        "timestamp": time.time(),
        "message": message,
        "runId": str(uuid.uuid4()),
        "taskId": "error",
    }

    async def _gen():
        yield f"event: RUN_ERROR\ndata: {json.dumps(error_event)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        status_code=status_code,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
