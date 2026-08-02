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
from typing import Any, cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from agui_on_acp.agui.events import AguiEvent, StateSnapshotEvent
from agui_on_acp.agui.sse import event_stream
from agui_on_acp.config import is_cwd_allowed
from agui_on_acp.sessions.manager import (
    ActiveSession,
    CwdRecordNotFoundError,
    ResumeUnsupportedError,
    SessionResumeFailedError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class ToolCall(BaseModel):
    """A tool-call descriptor in an AG-UI message."""

    id: str
    type: str = "function"
    function: dict[str, Any] = {}


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
    state: dict[str, Any] = {}
    messages: list[AgUiMessage] = []
    tools: list[dict[str, Any]] = []
    context: list[Any] = []
    forwardedProps: dict[str, Any] = {}
    resume: list[ResumeEntry] = []


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
        return _json_error("Session manager not initialized", status_code=500)

    thread_id = body.threadId
    if not thread_id:
        return _json_error(
            "threadId is required — create a session first via POST /ag-ui/sessions",
            status_code=400,
        )

    # Mode/model/configOptions are read from AG-UI's native ``state`` channel —
    # persisted client-side, resent on every run (fresh prompt and resume
    # alike) via ``agent.setState()`` / CopilotKit's ``useCoAgent``. The bridge
    # diffs against its last-applied baseline and only fires ``set_*`` for
    # actual changes, so unchanged ``state`` resent on every run is a no-op.
    # ``forwardedProps.mcpServers`` is NOT pulled in here — it's a create/
    # resume-time session parameter, not a per-turn UI setting, and has no
    # ``STATE_SNAPSHOT`` round-trip (see proposals/state-based-session-config.md).
    mode, model, config_options = _resolve_session_options(body)

    # ── Resume path ──────────────────────────────────────────────────────
    if body.resume:
        try:
            actual_run_id = await manager.resume_run(
                thread_id,
                [r.model_dump() for r in body.resume],
                mode=mode,
                model=model,
                config_options=config_options,
            )
        except KeyError:
            return _json_error(
                f"No active session for thread {thread_id} — that action expired, please try again",
                status_code=404,
            )
        except ValueError as exc:
            # No pending interrupt to resume — surface as a clear error instead
            # of a hanging empty stream.
            return _json_error(str(exc), status_code=409)

        queue = manager.get_event_queue(thread_id, actual_run_id)
        if queue is None:
            return _json_error("No event queue for resume run", status_code=500)
        return _sse_response(queue, thread_id, manager)

    # ── Fresh prompt on an existing/resumed session ─────────────────────────
    fp = body.forwardedProps
    # Resolve cwd from the bridge's durable ``session_id → cwd`` record so
    # the client doesn't need to resend it (``forwardedProps.cwd`` is accepted
    # for backward compatibility but ignored in favour of the stored record).
    try:
        cwd = await manager.resolve_cwd(thread_id)
    except CwdRecordNotFoundError as exc:
        # Bridge has no cwd record for this id (created before the
        # cwd-persistence store shipped, or by something other than this
        # bridge) — surface the specific reason rather than a bare "no session".
        return _json_error(str(exc), status_code=404)
    if not is_cwd_allowed(cwd):
        return _json_error("cwd not allowed", status_code=403)

    try:
        active = await manager.attach_for_prompt(thread_id, cwd, fp.get("mcpServers"))
    except ResumeUnsupportedError:
        return _json_error(
            f"No active session for thread {thread_id} and this agent does"
            " not support session/resume",
            status_code=409,
        )
    except SessionResumeFailedError:
        return _json_error(
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
        return _json_error("No user message provided", status_code=400)

    # Start a run
    try:
        actual_run_id = await manager.start_run(
            thread_id,
            {"messages": [{"role": "user", "content": user_message}]},
        )
    except Exception:  # pylint: disable=broad-exception-caught
        # Any failure to start the run (agent down, transport error, etc.)
        # is reported to the client as a 500 rather than crashing the
        # endpoint; the full stack trace is logged for server-side diagnosis.
        logger.exception("Failed to start run for thread %s", thread_id)
        return _json_error("failed to start run", status_code=500)

    # Apply mode/model/configOptions (from ``state`` — resolved above) BEFORE
    # emitting the STATE_SNAPSHOT. This is the sanctioned AG-UI mechanism for
    # changing mode/model/config mid-conversation: the bridge diffs against
    # its last-applied baseline and only fires ``set_*`` for actual changes,
    # so unchanged ``state`` resent on every run is a no-op. It reuses the
    # same best-effort policy as create-time application
    # (``_apply_session_options``): a single bad option is logged and
    # skipped, never aborting the run. Must run after ``start_run`` attached
    # the run's queue to the bridge — if the agent reflects a mode/config
    # change back as a ``session/update`` notification it needs a live queue
    # to land in (same ordering constraint as the snapshot below).
    await manager.apply_session_options(thread_id, mode, model, config_options)

    # Emit a STATE_SNAPSHOT with available modes/models AFTER start_run has
    # attached the bridge to the run's queue — emitting it before
    # start_run (the previous placement) dropped it because the bridge's
    # _queue was still None.
    _emit_state_snapshot(active)

    queue = manager.get_event_queue(thread_id, actual_run_id)
    if queue is None:
        return _json_error("No event queue for run", status_code=500)

    return _sse_response(queue, thread_id, manager)


def _resolve_session_options(
    body: RunAgentInput,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Pull mode/model/configOptions from AG-UI's native ``state`` channel.

    ``state`` is persisted client-side (set once via ``agent.setState()``,
    resent on every run, fresh prompt or resume) — it's the AG-UI mechanism
    designed for exactly this "set it once, it round-trips" usage pattern
    (see ``CopilotKit``'s ``useCoAgent({ state, setState })``). The bridge
    reads only ``state.mode`` / ``state.model`` / ``state.configOptions``;
    every other ``state`` key is ignored as opaque, client-owned domain data
    (consistent with the prior blanket ``state | ignored`` policy — only the
    three config keys are now load-bearing).
    """
    state = body.state
    mode: str | None = state.get("mode")
    model: str | None = state.get("model")
    config_options_raw = state.get("configOptions")
    config_options: dict[str, Any] | None = (
        cast(dict[str, Any], config_options_raw)
        if isinstance(config_options_raw, dict)
        else None
    )
    return mode, model, config_options


def _emit_state_snapshot(active: ActiveSession) -> None:
    """Emit a ``STATE_SNAPSHOT`` advertising the session's modes/models/
    config options AND the bridge's evolving plan/usage/sessionInfo state
    on the run's queue.

    This establishes the full state baseline at run start: mode/model/
    configOptions (the diff-driven config surface) plus plans/usage/
    sessionInfo (the evolving current-value state the bridge tracks across
    runs within a session). Pre-populating the latter three paths (even as
    ``{}``/``None`` on the first run) keeps the mid-turn STATE_DELTA
    `replace` ops spec-correct under a strict RFC 6902 applier — see
    ``proposals/plan-usage-session-info-as-state.md``.

    Must be called AFTER ``start_run`` / ``attach_resume_queue`` attached the
    bridge's emit queue — emitting earlier drops the event (the bridge's
    ``_queue`` is still ``None``). Accessing the bridge's internal ``_emit``
    is intentional here rather than widening the bridge API with a public
    snapshot emitter for this one call site.
    """
    snapshot: dict[str, Any] = {}
    if active.modes:
        snapshot["modes"] = active.modes
    if active.models:
        snapshot["models"] = active.models
    if active.config_options:
        snapshot["configOptions"] = active.config_options
    if active.current_mode_id:
        snapshot["currentModeId"] = active.current_mode_id
    # Bridge-tracked evolving state (plans/usage/sessionInfo). Always
    # included — even when empty — so the paths the mid-turn STATE_DELTA
    # deltas `replace` exist from the very first run.
    snapshot.update(active.bridge.extra_state())
    # Accessing the bridge's internal ``_emit`` is intentional here
    # rather than widening the bridge API with a public snapshot
    # emitter for this one call site.
    active.bridge._emit(  # pylint: disable=protected-access  # pyright: ignore[reportPrivateUsage]
        StateSnapshotEvent(snapshot=snapshot)
    )


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


def _json_error(message: str, *, status_code: int = 500) -> JSONResponse:
    """Pre-stream error: a plain JSON ``{"error": ...}`` body.

    Used for failures that happen BEFORE any SSE stream is opened (unknown
    session, unsupported capability, cwd not allowed, no user message).
    Mid-stream failures (errors that occur after a 200 +
    ``text/event-stream`` has started) are surfaced as ``RUN_ERROR`` events
    on the stream itself, not here — the client is already committed to
    parsing SSE in that case.
    """
    return JSONResponse({"error": message}, status_code=status_code)
