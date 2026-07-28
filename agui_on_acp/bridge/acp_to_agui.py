"""AcpToAguiBridge — implements the acp.Client protocol to translate SDK callbacks into AG-UI events.

This is the core of the bridge architecture. It maintains per-run state
(open message, open tool calls) and emits properly sequenced AG-UI events
into an asyncio.Queue that the SSE endpoint drains.

The bridge satisfies the acp.Client Protocol structurally:
    - session_update(session_id, update) — handles streaming updates
    - request_permission(session_id, tool_call, options) — handles tool approval
    - ext_notification(method, params) — handles _kiro.dev/* extensions
    - ext_method(method, params) — handles vendor extension methods
    - read_text_file, write_text_file — file operations for the agent
    - create_terminal, terminal_output, etc. — terminal operations
    - create_elicitation, complete_elicitation — ACP 0.11 elicitation (stubbed)
    - on_connect(conn) — called when the connection is established

Per-run state machine
---------------------

The bridge holds a small amount of per-run state so that ACP's frameless
delta stream can be translated into AG-UI's framed START/CONTENT/END
events. ``start_run()`` resets the state; ``attach_resume_queue()``
resets the message slot but deliberately preserves ``_open_tool_calls``
across the suspend/resume boundary (the tool call that triggered the
permission is still open and continues after resume).

::

                start_run()
                    │
                    ▼
          ┌─────────────────┐
          │  No Open State  │
          └────────┬────────┘
                   │
      agent_message_chunk
                   │
                   ▼
          ┌─────────────────┐
          │  Message Open   │──── agent_message_chunk ──→ (emit CONTENT)
          └────────┬────────┘
                   │
              tool_call
                   │
  (close message: emit END)
                   │
                   ▼
          ┌─────────────────┐
          │  Tool Call Open │──── tool_call_update ──→ (emit ARGS/END)
          └────────┬────────┘
                   │
              turn_end
                   │
  (close all: emit FINISHED)
                   │
                   ▼
          ┌─────────────────┐
          │    Run Done     │
          └─────────────────┘

Sequencing rules
----------------

1. **Only one text message can be open at a time.** If a ``tool_call``
   arrives while a message is open, the bridge emits ``TEXT_MESSAGE_END``
   first.
2. **Multiple tool calls can be open simultaneously.** Each gets its own
   ``TOOL_CALL_START`` and is tracked by id in ``_open_tool_calls`` until
   ``TOOL_CALL_END``.
3. **``turn_end`` closes everything.** Any open message gets
   ``TEXT_MESSAGE_END``, all open tool calls get ``TOOL_CALL_END`` (plus
   a synthesised empty ``TOOL_CALL_RESULT`` so CopilotKit's renderer can
   flip orphaned calls to "complete"), then ``RUN_FINISHED`` is emitted.
4. **Vendor extensions arriving before the first run are buffered** in
   ``_pending_notifications`` and flushed as ``CUSTOM`` events when
   ``start_run()`` / ``attach_resume_queue()`` is called — otherwise
   session-init notifications would be lost (no SSE stream exists yet).

For the full ACP ↔ AG-UI field mapping (including the interrupt/resume
permission flow and every dropped ACP 0.11 variant) see
``docs/agui-acp-mapping.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, cast

import acp
import acp.schema

from agui_on_acp.agui.events import (
    AguiEvent,
    CustomEvent,
    Interrupt,
    InterruptOutcome,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

logger = logging.getLogger(__name__)

# TTL for parked permission futures. If no resume arrives within this window
# (e.g. the user closed the tab), the future resolves with `cancelled` so the
# prompt task unwinds instead of hanging forever (leaking the ACP subprocess).
PERMISSION_TTL_SECONDS = 300.0


class AcpToAguiBridge:
    """Stateful translator from ACP SDK callbacks to AG-UI events.

    Satisfies the acp.Client Protocol so the SDK routes session_update,
    request_permission, ext_notification, and file/terminal callbacks here.

    One bridge instance is created per task and reused across runs.
    Call ``start_run()`` to reset per-run state and connect to a new queue.
    """

    def __init__(
        self,
        task_id: str,
    ) -> None:
        self.task_id = task_id
        self._log = logging.LoggerAdapter(logger, {"task_id": task_id})

        # Per-run state — reset on start_run()
        self._run_id: str | None = None
        self._queue: asyncio.Queue[AguiEvent] | None = None
        self._current_message_id: str | None = None
        self._has_open_message: bool = False
        self._open_tool_calls: set[str] = set()

        # Session-level notification buffer — holds _kiro.dev/* notifications
        # that arrive before any run starts (e.g. during session init).
        # Flushed as CUSTOM events when the first run begins.
        self._pending_notifications: list[tuple[str, dict[str, Any]]] = []

        # Permission futures — maps call_id to asyncio.Future that
        # request_permission awaits. Resolved by resume_run (AG-UI resume) or
        # cancel_run. Correlation invariant: the call_id equals the interrupt
        # id and the ACP permission tool callId — one key through the flow.
        self._permission_futures: dict[
            str, asyncio.Future[acp.RequestPermissionResponse]
        ] = {}
        # Per-future TTL handles so we can cancel the expiry timer when a
        # resume/cancel resolves the future first.
        self._permission_timers: dict[str, asyncio.TimerHandle] = {}

        # Elicitation futures — same suspend/resume pattern as permissions,
        # but for ACP 0.11 ``create_elicitation``. Keyed by the elicitation
        # id (taken from the request for URL mode, or generated for form
        # mode). The interrupt id surfaced to the AG-UI client === this key.
        self._elicitation_futures: dict[str, asyncio.Future[Any]] = {}
        self._elicitation_timers: dict[str, asyncio.TimerHandle] = {}

        # Working directory for file operations (set by session manager)
        self._cwd: str = ""

        # Log collapsing for streaming chunks
        self._content_chunk_count: int = 0

    @property
    def cwd(self) -> str:
        """Working directory used to resolve relative file paths."""
        return self._cwd

    @cwd.setter
    def cwd(self, value: str) -> None:
        self._cwd = value

    @property
    def run_id(self) -> str | None:
        """The current run id, or None when no run is active."""
        return self._run_id

    # ── Run lifecycle ────────────────────────────────────────────────────────

    def start_run(self, run_id: str, queue: asyncio.Queue[AguiEvent]) -> None:
        """Begin a new run — reset state and emit RUN_STARTED."""
        self._run_id = run_id
        self._queue = queue
        self._current_message_id = None
        self._has_open_message = False
        self._open_tool_calls.clear()

        self._emit(
            RunStartedEvent(
                runId=run_id,
                taskId=self.task_id,
                threadId=self.task_id,
            )
        )

        # Flush any buffered session-level notifications as CUSTOM events
        if self._pending_notifications:
            self._log.debug(
                "Flushing %d buffered notifications into run %s",
                len(self._pending_notifications),
                run_id,
            )
            for method, params in self._pending_notifications:
                self._handle_agent_extension(method, params)
            self._pending_notifications.clear()

    def finish_run(self) -> None:
        """Explicitly finish the current run (e.g. on turn_end)."""
        self._close_open_message()
        self._close_all_tool_calls()
        if self._run_id:
            self._emit(
                RunFinishedEvent(
                    runId=self._run_id, taskId=self.task_id, threadId=self.task_id
                )
            )
        self._run_id = None

    def error_run(self, message: str, code: str | None = None) -> None:
        """Emit RUN_ERROR and close the run."""
        self._close_open_message()
        self._close_all_tool_calls()
        if self._run_id:
            self._emit(
                RunErrorEvent(
                    runId=self._run_id,
                    taskId=self.task_id,
                    message=message,
                    code=code,
                    threadId=self.task_id,
                )
            )
        self._run_id = None

    def attach_resume_queue(self, run_id: str, queue: asyncio.Queue[AguiEvent]) -> None:
        """Re-attach a new SSE stream to a prompt task suspended at a
        permission Future.

        Unlike ``start_run``, this does NOT reset ``_open_tool_calls`` — the
        tool call that triggered the permission is still open and continues
        across the suspend/resume boundary. Emits RUN_STARTED so the new SSE
        stream has a clean start, then flushes any buffered notifications.
        """
        self._run_id = run_id
        self._queue = queue
        self._has_open_message = False

        self._emit(
            RunStartedEvent(
                runId=run_id,
                taskId=self.task_id,
                threadId=self.task_id,
            )
        )

        if self._pending_notifications:
            self._log.debug(
                "Flushing %d buffered notifications into resume run %s",
                len(self._pending_notifications),
                run_id,
            )
            for method, params in self._pending_notifications:
                self._handle_agent_extension(method, params)
            self._pending_notifications.clear()

    def _suspend_run(self, interrupt: Interrupt) -> None:
        """End the current SSE stream with an interrupt outcome, WITHOUT
        closing tool calls or going through ``finish_run`` (which would emit a
        second outcome-less RUN_FINISHED and close tool calls we want to keep
        open across the suspend).

        The prompt task remains parked at ``await future`` in
        ``request_permission``; ``attach_resume_queue`` re-attaches a new
        stream on resume.
        """
        self._close_open_message()
        if self._run_id:
            self._emit(
                RunFinishedEvent(
                    runId=self._run_id,
                    taskId=self.task_id,
                    threadId=self.task_id,
                    outcome=InterruptOutcome(interrupts=[interrupt]),
                )
            )
        self._run_id = None
        self._queue = None

    # ── acp.Client Protocol — Core callbacks ─────────────────────────────────

    def on_connect(self, conn: Any) -> None:
        """Called when the connection is established."""
        self._log.info("ACP connection established")

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        """Handle streaming updates from the SDK.

        The `update` is a typed object (AgentMessageChunk, ToolCallStart,
        ToolCallProgress, AvailableCommandsUpdate, CurrentModeUpdate, etc.)
        """
        if self._queue is None:
            self._log.warning(
                "session_update received but no active run (session=%s)", session_id
            )
            return

        # If the update is a dict (fallback), handle it the old way
        if isinstance(update, dict):
            self._handle_session_update_dict(cast(dict[str, Any], update))
            return

        # Handle typed SDK objects
        update_type = type(update).__name__
        if not isinstance(update, acp.schema.AgentMessageChunk):
            self._log.info("recv %s", update_type)

        if isinstance(update, acp.schema.AgentMessageChunk):
            self._handle_agent_message_chunk_typed(update)
        elif isinstance(update, acp.schema.ToolCallStart):
            self._handle_tool_call_typed(update)
        elif isinstance(update, acp.schema.ToolCallProgress):
            self._handle_tool_call_update_typed(update)
        elif isinstance(update, acp.schema.CurrentModeUpdate):
            mode_id = getattr(update, "mode_id", "") or getattr(update, "modeId", "")
            self._emit(
                CustomEvent(
                    name="agent:mode_update",
                    value={"modeId": mode_id},
                )
            )
        elif isinstance(update, acp.schema.AvailableCommandsUpdate):
            commands = getattr(update, "commands", [])
            self._emit(
                CustomEvent(
                    name="agent:commands_available",
                    value={"commands": commands},
                )
            )
        elif isinstance(update, acp.schema.ConfigOptionUpdate):
            # ACP 0.11: the notification carries the full set of config
            # options and their current values — emit a STATE_SNAPSHOT that
            # replaces whatever the client previously held.
            self._emit(
                StateSnapshotEvent(
                    snapshot={
                        "configOptions": serialize_config_options(
                            getattr(update, "config_options", [])
                        )
                    }
                )
            )
        elif isinstance(update, acp.schema.UsageUpdate):
            value: dict[str, Any] = {
                "used": getattr(update, "used", 0),
                "size": getattr(update, "size", 0),
            }
            cost = getattr(update, "cost", None)
            if cost is not None:
                value["cost"] = _model_to_dict(cost)
            self._emit(CustomEvent(name="agent:usage", value=value))
        elif isinstance(update, acp.schema.SessionInfoUpdate):
            self._emit(
                CustomEvent(
                    name="agent:session_info",
                    value=_model_to_dict(update),
                )
            )
        elif isinstance(update, acp.schema.AgentPlanUpdate):
            self._emit(
                CustomEvent(
                    name="agent:plan",
                    value={"entries": _model_to_dict(getattr(update, "entries", []))},
                )
            )
        elif isinstance(update, acp.schema.AgentPlanContentUpdate):
            self._emit(
                CustomEvent(
                    name="agent:plan_update",
                    value=_model_to_dict(getattr(update, "plan", None)),
                )
            )
        elif isinstance(update, acp.schema.AgentPlanRemovedUpdate):
            self._emit(
                CustomEvent(
                    name="agent:plan_removed",
                    value={"id": getattr(update, "id", "")},
                )
            )
        elif isinstance(update, acp.schema.AgentThoughtChunk):
            content = getattr(update, "content", None)
            thought_text = getattr(content, "text", "") if content else ""
            self._emit(
                CustomEvent(name="agent:thought", value={"delta": thought_text or ""})
            )
        else:
            # Fallback: try to extract as dict
            if hasattr(update, "model_dump"):
                self._handle_session_update_dict(
                    cast(dict[str, Any], update.model_dump(by_alias=True))
                )
            elif hasattr(update, "__dict__"):
                self._handle_session_update_dict(cast(dict[str, Any], vars(update)))
            else:
                self._log.debug(
                    "Unhandled session_update type: %s", type(update).__name__
                )

    async def request_permission(
        self, session_id: str, tool_call: Any, options: Any, **kwargs: Any
    ) -> acp.RequestPermissionResponse:
        """Handle tool approval requests from the SDK.

        Emits a ``RUN_FINISHED{outcome:interrupt}`` so the AG-UI client
        (CopilotKit ``useInterrupt``) surfaces an approval UI, then parks a
        Future and suspends. The SSE stream closes on the interrupt
        ``RUN_FINISHED``; a subsequent resume run re-attaches a stream and
        resolves the Future, unblocking the prompt task.
        """
        # Extract info from tool_call
        if isinstance(tool_call, dict):
            tc_dict = cast(dict[str, Any], tool_call)
            tool_call_id = str(
                tc_dict.get("toolCallId") or tc_dict.get("tool_call_id") or uuid.uuid4()
            )
            tool_name = str(
                tc_dict.get("title")
                or tc_dict.get("toolName")
                or tc_dict.get("tool_name")
                or "unknown"
            )
        else:
            tool_call_id = str(
                getattr(tool_call, "tool_call_id", None)
                or getattr(tool_call, "toolCallId", None)
                or uuid.uuid4()
            )
            tool_name = str(
                getattr(tool_call, "title", None)
                or getattr(tool_call, "tool_name", None)
                or getattr(tool_call, "toolName", None)
                or "unknown"
            )

        # Extract options list for the client UI
        options_list: list[Any]
        if isinstance(options, list):
            options_list = cast(list[Any], options)
        elif hasattr(options, "__iter__"):
            options_list = [opt for opt in options]
        else:
            options_list = []

        serialized_options: list[Any] = []
        for opt in options_list:
            if isinstance(opt, dict):
                serialized_options.append(opt)
            elif hasattr(opt, "model_dump"):
                serialized_options.append(opt.model_dump(by_alias=True))
            elif hasattr(opt, "__dict__"):
                serialized_options.append(vars(opt))
            else:
                serialized_options.append(str(opt))

        # Compute an expiry deadline shared by the interrupt and the Future TTL
        # so the AG-UI client guard (agent.ts:407-411) and our server-side
        # cleanup agree.
        loop = asyncio.get_event_loop()
        import datetime as _dt

        expires_at_iso = _dt.datetime.fromtimestamp(
            _dt.datetime.now().timestamp() + PERMISSION_TTL_SECONDS,
            tz=_dt.timezone.utc,
        ).isoformat()

        interrupt = Interrupt(
            id=tool_call_id,
            reason="tool_call",
            toolCallId=tool_call_id,
            message=f"Permission required: {tool_name}",
            expiresAt=expires_at_iso,
            metadata={
                "toolName": tool_name,
                "options": serialized_options,
            },
        )

        # Park the Future BEFORE emitting the interrupt outcome — the outcome
        # closes the SSE stream, so the Future must exist when a resume
        # arrives. ``_suspend_run`` nulls _queue/_run_id; the prompt task then
        # suspends at ``await future`` below.
        future: asyncio.Future[acp.RequestPermissionResponse] = loop.create_future()
        self._permission_futures[tool_call_id] = future

        # Schedule a TTL expiry: if no resume arrives, resolve with cancelled
        # so the prompt unwinds instead of hanging.
        timer = loop.call_later(
            PERMISSION_TTL_SECONDS,
            self._expire_permission,
            tool_call_id,
        )
        self._permission_timers[tool_call_id] = timer

        self._log.info("⏸ interrupting for %s (callId=%s)", tool_name, tool_call_id)
        self._suspend_run(interrupt)

        response = await future
        self._log.info(
            "✓ permission resolved for %s → %s",
            tool_name,
            getattr(response, "outcome", "?"),
        )
        return response

    def _expire_permission(self, call_id: str) -> None:
        """TTL callback — resolve a parked permission Future as cancelled if
        no resume arrived in time."""
        future = self._permission_futures.pop(call_id, None)
        self._permission_timers.pop(call_id, None)
        if future is None or future.done():
            return
        self._log.warning(
            "permission future %s expired (no resume) → cancelled", call_id
        )
        response = acp.RequestPermissionResponse(
            outcome=acp.schema.DeniedOutcome(outcome="cancelled")
        )
        future.set_result(response)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Handle vendor extension notifications like _kiro.dev/*.

        These become CUSTOM AG-UI events.
        """
        if self._queue is None:
            # No active run — buffer for later
            if method.startswith("_kiro.dev/") or method == "_session/terminate":
                self._log.debug(
                    "Buffering ext_notification (no active run): %s", method
                )
                self._pending_notifications.append((method, params))
            return

        self._handle_agent_extension(method, params)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle vendor extension method calls.

        Return empty dict for unhandled methods.
        """
        self._log.debug("ext_method called: %s", method)
        return {}

    # ── acp.Client Protocol — File operations ────────────────────────────────

    async def read_text_file(
        self,
        session_id: str,
        path: str,
        line: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> acp.ReadTextFileResponse:
        """Read a text file on behalf of the agent."""
        self._log.debug("read_text_file: %s", path)
        try:
            full_path = (
                os.path.join(self._cwd, path) if not os.path.isabs(path) else path
            )
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                if line is not None:
                    lines = f.readlines()
                    start = max(0, line - 1)
                    end = start + (limit or len(lines))
                    content = "".join(lines[start:end])
                elif limit is not None:
                    content = f.read(limit)
                else:
                    content = f.read()
            return acp.ReadTextFileResponse(content=content)
        except Exception as exc:
            self._log.error("read_text_file failed: %s", exc)
            return acp.ReadTextFileResponse(content=f"Error reading file: {exc}")

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> acp.WriteTextFileResponse | None:
        """Write a text file on behalf of the agent."""
        self._log.debug("write_text_file: %s", path)
        try:
            full_path = (
                os.path.join(self._cwd, path) if not os.path.isabs(path) else path
            )
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            return acp.WriteTextFileResponse()
        except Exception as exc:
            self._log.error("write_text_file failed: %s", exc)
            return None

    # ── acp.Client Protocol — Terminal operations ────────────────────────────

    async def create_terminal(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: Any = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> acp.CreateTerminalResponse:
        """Create a terminal process for the agent.

        For now, we generate a terminal ID. Full terminal management is
        handled by the agent itself in most cases.
        """
        terminal_id = str(uuid.uuid4())
        self._log.info(
            "create_terminal: %s %s (id=%s)", command, args or [], terminal_id
        )
        return acp.CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> acp.TerminalOutputResponse:
        """Get terminal output."""
        return acp.TerminalOutputResponse(output="", truncated=False)

    async def release_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> acp.ReleaseTerminalResponse | None:
        """Release a terminal."""
        return acp.ReleaseTerminalResponse()

    async def wait_for_terminal_exit(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> acp.WaitForTerminalExitResponse:
        """Wait for a terminal to exit."""
        return acp.WaitForTerminalExitResponse(exit_code=0)

    async def kill_terminal(
        self, session_id: str, terminal_id: str, **kwargs: Any
    ) -> acp.KillTerminalResponse | None:
        """Kill a terminal."""
        return acp.KillTerminalResponse()

    # ── acp.Client Protocol — Elicitation (ACP 0.11) ─────────────────────────
    # Elicitations are surfaced to the AG-UI client as interrupts with
    # ``reason: "elicitation"``, reusing the same suspend/resume plumbing as
    # ``request_permission``: park a Future, end the SSE stream with an
    # interrupt outcome, and resolve the Future when the client resumes.

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        """Surface an ACP elicitation as an AG-UI interrupt.

        Parks a Future keyed by the elicitation id (taken from ``mode`` for
        URL elicitations, or generated for form elicitations which carry no
        id in the protocol), emits ``RUN_FINISHED{outcome:interrupt}`` with
        ``reason="elicitation"`` and a ``responseSchema`` derived from the
        requested schema, then suspends until the client resumes.
        """
        elicitation_id = _elicitation_id_from_mode(mode) or str(uuid.uuid4())
        requested_schema = _elicitation_schema_from_mode(mode)
        mode_kind = _elicitation_mode_kind(mode)

        # Build a JSON-Schema-shaped object the client can render a form from.
        response_schema: dict[str, Any] | None = None
        if requested_schema is not None:
            response_schema = _model_to_dict(requested_schema)

        import datetime as _dt

        expires_at_iso = _dt.datetime.fromtimestamp(
            _dt.datetime.now().timestamp() + PERMISSION_TTL_SECONDS,
            tz=_dt.timezone.utc,
        ).isoformat()

        interrupt = Interrupt(
            id=elicitation_id,
            reason="elicitation",
            message=message,
            responseSchema=response_schema,
            expiresAt=expires_at_iso,
            metadata={
                "mode": mode_kind,
                "elicitationId": elicitation_id,
            },
        )

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._elicitation_futures[elicitation_id] = future
        timer = loop.call_later(
            PERMISSION_TTL_SECONDS, self._expire_elicitation, elicitation_id
        )
        self._elicitation_timers[elicitation_id] = timer

        self._log.info(
            "⏸ interrupting for elicitation %s (id=%s)", mode_kind, elicitation_id
        )
        self._suspend_run(interrupt)

        response = await future
        self._log.info(
            "✓ elicitation resolved (id=%s) → %s",
            elicitation_id,
            getattr(response, "action", "?"),
        )
        return response

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        """The agent notified that a previously-started elicitation completed
        mid-stream. Surface it as a CUSTOM event so clients can react (rare;
        usually the accept/decline reply closes the loop)."""
        self._emit(
            CustomEvent(
                name="agent:elicitation_complete",
                value={"elicitationId": elicitation_id},
            )
        )

    def _expire_elicitation(self, elicitation_id: str) -> None:
        """TTL callback — resolve a parked elicitation Future as cancelled."""
        future = self._elicitation_futures.pop(elicitation_id, None)
        self._elicitation_timers.pop(elicitation_id, None)
        if future is None or future.done():
            return
        self._log.warning(
            "elicitation future %s expired (no resume) → cancelled", elicitation_id
        )
        future.set_result(acp.CancelElicitationResponse(action="cancel"))

    # ── Permission resolution (called by SessionManager on resume/cancel) ────

    def resolve_permission(
        self, call_id: str, approved: bool, option_id: str | None = None
    ) -> bool:
        """Resolve a pending permission future.

        Called by ``SessionManager.resume_run`` (AG-UI resume) or
        ``cancel_run``. Returns True if a pending future was found and
        resolved, False if there was nothing to resolve (e.g. unknown
        interrupt id / already expired).
        """
        future = self._permission_futures.pop(call_id, None)
        timer = self._permission_timers.pop(call_id, None)
        if timer is not None:
            timer.cancel()

        if future is None:
            self._log.warning("No pending permission future for call_id=%s", call_id)
            return False
        if future.done():
            self._log.warning("Permission future for call_id=%s already done", call_id)
            return False

        if approved:
            # OpenCode's option IDs are once/always/reject, NOT allow_once.
            outcome: acp.schema.DeniedOutcome | acp.schema.AllowedOutcome = (
                acp.schema.AllowedOutcome(
                    option_id=option_id or "once", outcome="selected"
                )
            )
        else:
            outcome = acp.schema.DeniedOutcome(outcome="cancelled")

        response = acp.RequestPermissionResponse(outcome=outcome)
        future.set_result(response)
        return True

    def pending_interrupt_ids(self) -> list[str]:
        """Return the ids of all parked (unresolved) permission and
        elicitation futures."""
        return [
            cid for cid, fut in self._permission_futures.items() if not fut.done()
        ] + [cid for cid, fut in self._elicitation_futures.items() if not fut.done()]

    def cancel_all_permissions(self) -> None:
        """Resolve every parked permission and elicitation future as cancelled.

        Used by ``cancel_run`` so ``request_permission`` / ``create_elicitation``
        unblock and the prompt task unwinds instead of hanging on a dead
        session.
        """
        for call_id in list(self._permission_futures.keys()):
            self.resolve_permission(call_id, approved=False)
        for elicitation_id in list(self._elicitation_futures.keys()):
            self.resolve_elicitation(elicitation_id, status="cancelled")

    def resolve_interrupt(self, interrupt_id: str, status: str, payload: Any) -> bool:
        """Resolve a parked interrupt (permission or elicitation) by id.

        Dispatches to the right table based on which future the id belongs
        to. Returns True if a pending future was found and resolved.
        """
        if interrupt_id in self._permission_futures:
            if status == "cancelled":
                return self.resolve_permission(interrupt_id, approved=False)
            if isinstance(payload, str):
                option_id: str = payload
            elif isinstance(payload, dict):
                option_id = str(cast(dict[str, Any], payload).get("optionId", "once"))
            else:
                option_id = "once"
            return self.resolve_permission(
                interrupt_id, approved=True, option_id=option_id
            )
        if interrupt_id in self._elicitation_futures:
            return self.resolve_elicitation(
                interrupt_id, status=status, payload=payload
            )
        self._log.warning("No pending interrupt future for id=%s", interrupt_id)
        return False

    def resolve_elicitation(
        self, elicitation_id: str, status: str, payload: Any = None
    ) -> bool:
        """Resolve a parked elicitation future with the matching ACP response.

        ``status`` is the AG-UI resume status: ``"resolved"`` (accepted),
        ``"cancelled"``, or anything else mapped to declined. The payload for
        an accepted elicitation is ``{"status": "accepted", "values": {...}}``
        or simply the values dict.
        """
        future = self._elicitation_futures.pop(elicitation_id, None)
        timer = self._elicitation_timers.pop(elicitation_id, None)
        if timer is not None:
            timer.cancel()
        if future is None:
            self._log.warning("No pending elicitation future for id=%s", elicitation_id)
            return False
        if future.done():
            self._log.warning(
                "Elicitation future for id=%s already done", elicitation_id
            )
            return False

        if status == "cancelled":
            response: Any = acp.CancelElicitationResponse(action="cancel")
        else:
            # Accepted (status == "resolved") or declined, based on payload.
            values: dict[str, Any] | None = None
            if isinstance(payload, dict):
                payload_dict = cast(dict[str, Any], payload)
                if "values" in payload_dict and isinstance(
                    payload_dict["values"], dict
                ):
                    values = cast(dict[str, Any], payload_dict["values"])
                elif "status" in payload_dict:
                    # {"status": "accepted"/"declined"/"cancelled", "values"?}
                    inner = str(payload_dict.get("status", ""))
                    if inner == "declined":
                        response = acp.DeclineElicitationResponse(action="decline")
                        future.set_result(response)
                        return True
                    if inner == "cancelled":
                        response = acp.CancelElicitationResponse(action="cancel")
                        future.set_result(response)
                        return True
                    values = (
                        cast(dict[str, Any], payload_dict.get("values"))
                        if isinstance(payload_dict.get("values"), dict)
                        else None
                    )
                else:
                    # Treat the dict itself as the form values.
                    values = payload_dict
            if values is not None:
                response = acp.AcceptElicitationResponse(
                    action="accept", content=values
                )
            else:
                # No values supplied — decline rather than fabricate content.
                response = acp.DeclineElicitationResponse(action="decline")
        future.set_result(response)
        return True

    # ── Fallback dict-based session/update handling ──────────────────────────

    def _handle_session_update_dict(self, update: dict[str, Any]) -> None:
        """Handle session/update when received as a raw dict (fallback)."""
        kind = update.get("sessionUpdate") or update.get("session_update")
        if kind == "agent_message_chunk":
            self._handle_agent_message_chunk_dict(update)
        elif kind == "tool_call":
            self._handle_tool_call_dict(update)
        elif kind == "tool_call_update":
            self._handle_tool_call_update_dict(update)
        elif kind == "turn_end":
            self._handle_turn_end()
        elif kind == "current_mode_update":
            self._emit(
                CustomEvent(
                    name="agent:mode_update",
                    value={"modeId": update.get("modeId", update.get("mode_id", ""))},
                )
            )
        elif kind == "config_option_update":
            self._emit(
                StateSnapshotEvent(
                    snapshot={
                        "configOptions": serialize_config_options(
                            update.get(
                                "configOptions", update.get("config_options", [])
                            )
                        )
                    }
                )
            )
        elif kind == "usage_update":
            value: dict[str, Any] = {
                "used": update.get("used", 0),
                "size": update.get("size", 0),
            }
            if update.get("cost") is not None:
                value["cost"] = update.get("cost")
            self._emit(CustomEvent(name="agent:usage", value=value))
        elif kind == "session_info_update":
            self._emit(CustomEvent(name="agent:session_info", value=update))
        elif kind == "plan":
            self._emit(
                CustomEvent(
                    name="agent:plan",
                    value={"entries": update.get("entries", [])},
                )
            )
        elif kind == "plan_update":
            self._emit(
                CustomEvent(name="agent:plan_update", value=update.get("plan", update))
            )
        elif kind == "plan_removed":
            self._emit(
                CustomEvent(
                    name="agent:plan_removed", value={"id": update.get("id", "")}
                )
            )
        elif kind == "agent_thought_chunk":
            content = update.get("content", {})
            thought_text = (
                cast(dict[str, Any], content).get("text", "")
                if isinstance(content, dict)
                else ""
            )
            self._emit(CustomEvent(name="agent:thought", value={"delta": thought_text}))
        else:
            self._log.debug("Unhandled session/update kind: %s", kind)

    # ── Typed SDK update handlers ────────────────────────────────────────────

    def _handle_agent_message_chunk_typed(
        self, update: acp.schema.AgentMessageChunk
    ) -> None:
        """Handle AgentMessageChunk from the SDK."""
        content = getattr(update, "content", None)
        text = ""
        if content:
            text = getattr(content, "text", "") or ""
        if not text:
            return

        if not self._has_open_message:
            msg_id = str(uuid.uuid4())
            self._current_message_id = msg_id
            self._has_open_message = True
            self._emit(TextMessageStartEvent(messageId=msg_id))

        self._emit(
            TextMessageContentEvent(
                messageId=self._current_message_id,  # type: ignore[arg-type]
                delta=text,
            )
        )

    def _handle_tool_call_typed(self, update: acp.schema.ToolCallStart) -> None:
        """Handle ToolCallStart from the SDK."""
        self._close_open_message()

        tool_call_id = str(
            getattr(update, "tool_call_id", None)
            or getattr(update, "toolCallId", str(uuid.uuid4()))
        )
        tool_name = getattr(update, "title", None) or getattr(
            update, "tool_name", "unknown"
        )
        raw_input = getattr(update, "raw_input", None) or getattr(
            update, "rawInput", {}
        )

        if isinstance(raw_input, dict):
            cast(dict[str, Any], raw_input).pop("__tool_use_purpose", None)

        self._emit(
            ToolCallStartEvent(
                toolCallId=tool_call_id,
                toolCallName=tool_name,
                parentMessageId=self._current_message_id,
            )
        )
        self._open_tool_calls.add(tool_call_id)

        # opencode's ACP implementation doesn't populate raw_input for
        # read/glob/bash — only `kind` ("read"/"search"/"execute") and an
        # empty `locations` list are available at ToolCallStart time. Enrich
        # the args delta with what IS available so the renderer isn't a
        # blank `{}`. When raw_input is populated (e.g. bash's {cwd}), it
        # passes through unchanged.
        args_obj: dict[str, Any] = (
            cast(dict[str, Any], raw_input) if isinstance(raw_input, dict) else {}
        )
        kind = getattr(update, "kind", None)
        locations = getattr(update, "locations", None)
        if kind:
            args_obj.setdefault("kind", kind)
        if locations:
            args_obj.setdefault("locations", locations)
        args_json = json.dumps(args_obj) if args_obj else "{}"
        self._emit(
            ToolCallArgsEvent(
                toolCallId=tool_call_id,
                delta=args_json,
            )
        )
        # Approval is driven solely by the ACP request_permission callback,
        # which emits an interrupt RUN_FINISHED — no policy gate here.

    def _handle_tool_call_update_typed(
        self, update: acp.schema.ToolCallProgress
    ) -> None:
        """Handle ToolCallProgress from the SDK."""
        tool_call_id = str(
            getattr(update, "tool_call_id", None) or getattr(update, "toolCallId", "")
        )
        status = getattr(update, "status", "")
        # ACP carries the tool result in `raw_output`, not `result`. The old
        # code read a nonexistent `result` attribute, so every TOOL_CALL_RESULT
        # arrived with empty content.
        raw_output = getattr(update, "raw_output", None)
        result_obj = (
            raw_output if raw_output is not None else getattr(update, "result", None)
        )

        if status in ("completed", "failed"):
            if tool_call_id in self._open_tool_calls:
                result_str = self._serialize_tool_result(result_obj)
                self._emit(
                    ToolCallEndEvent(
                        toolCallId=tool_call_id,
                        result=result_str or None,
                    )
                )
                # TOOL_CALL_RESULT is what CopilotKit's runtime listens for to
                # synthesize a ToolMessage (role="tool") in its message store —
                # the renderer keys off that message to flip from inProgress to
                # complete. TOOL_CALL_END alone only signals end-of-args-streaming
                # and carries no message payload, so without this event the
                # renderer stays stuck at inProgress with empty parameters.
                self._emit(
                    ToolCallResultEvent(
                        messageId=f"{tool_call_id}-result",
                        toolCallId=tool_call_id,
                        content=result_str,
                    )
                )
                self._open_tool_calls.discard(tool_call_id)
        elif result_obj is not None:
            self._emit(
                ToolCallArgsEvent(
                    toolCallId=tool_call_id,
                    delta=json.dumps({"_progress": result_obj}),
                )
            )

    # ── Dict-based handlers (fallback for raw dict updates) ──────────────────

    def _handle_agent_message_chunk_dict(self, update: dict[str, Any]) -> None:
        """Translate agent_message_chunk dict to TEXT_MESSAGE_START/CONTENT."""
        content = update.get("content", {})
        text = content.get("text", "")
        if not text:
            return

        if not self._has_open_message:
            msg_id = str(uuid.uuid4())
            self._current_message_id = msg_id
            self._has_open_message = True
            self._emit(TextMessageStartEvent(messageId=msg_id))

        self._emit(
            TextMessageContentEvent(
                messageId=self._current_message_id,  # type: ignore[arg-type]
                delta=text,
            )
        )

    def _handle_tool_call_dict(self, update: dict[str, Any]) -> None:
        """Translate tool_call dict to TOOL_CALL_START + TOOL_CALL_ARGS."""
        self._close_open_message()

        tool_call_id = update.get("toolCallId", str(uuid.uuid4()))
        tool_name = update.get("title", update.get("toolName", "unknown"))
        raw_input = update.get("rawInput", {})

        raw_input.pop("__tool_use_purpose", None)

        self._emit(
            ToolCallStartEvent(
                toolCallId=tool_call_id,
                toolCallName=tool_name,
                parentMessageId=self._current_message_id,
            )
        )
        self._open_tool_calls.add(tool_call_id)

        args_json = json.dumps(raw_input)
        self._emit(
            ToolCallArgsEvent(
                toolCallId=tool_call_id,
                delta=args_json,
            )
        )
        # Approval is driven solely by the ACP request_permission callback.

    def _handle_tool_call_update_dict(self, update: dict[str, Any]) -> None:
        """Translate tool_call_update dict to TOOL_CALL_ARGS or TOOL_CALL_END."""
        tool_call_id = update.get("toolCallId", "")
        status = update.get("status", "")
        # Prefer raw_output (ACP field); fall back to result for legacy dicts.
        result_obj = update.get("raw_output")
        if result_obj is None:
            result_obj = update.get("result")

        if status in ("completed", "failed"):
            if tool_call_id in self._open_tool_calls:
                result_str = self._serialize_tool_result(result_obj)
                self._emit(
                    ToolCallEndEvent(
                        toolCallId=tool_call_id,
                        result=result_str or None,
                    )
                )
                # See _handle_tool_call_update_typed for rationale: emit a
                # TOOL_CALL_RESULT so CopilotKit synthesizes a ToolMessage and
                # the renderer can flip to "complete" with the actual output.
                self._emit(
                    ToolCallResultEvent(
                        messageId=f"{tool_call_id}-result",
                        toolCallId=tool_call_id,
                        content=result_str,
                    )
                )
                self._open_tool_calls.discard(tool_call_id)
        elif result_obj is not None:
            self._emit(
                ToolCallArgsEvent(
                    toolCallId=tool_call_id,
                    delta=json.dumps({"_progress": result_obj}),
                )
            )

    # ── Turn end ─────────────────────────────────────────────────────────────

    def _handle_turn_end(self) -> None:
        """Translate turn_end to close open message/tools + RUN_FINISHED."""
        self.finish_run()

    # ── Agent extension notifications to CUSTOM ──────────────────────────────

    def _handle_agent_extension(self, method: str, params: dict[str, Any]) -> None:
        """Map _kiro.dev/* and _session/* notifications to CUSTOM events."""
        name_map = {
            "_kiro.dev/metadata": "agent:metadata",
            "_kiro.dev/mcp/server_initialized": "agent:mcp_initialized",
            "_kiro.dev/mcp/oauth_request": "agent:mcp_oauth",
            "_kiro.dev/compaction/status": "agent:compaction",
            "_kiro.dev/clear/status": "agent:clear",
            "_kiro.dev/commands/available": "agent:commands_available",
            "_session/terminate": "agent:subagent_terminated",
        }
        event_name = name_map.get(
            method, f"agent:{method.replace('_kiro.dev/', '').replace('/', '_')}"
        )

        self._emit(CustomEvent(name=event_name, value=params))

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _close_open_message(self) -> None:
        """Close the current text message if one is open."""
        if self._has_open_message and self._current_message_id:
            self._emit(TextMessageEndEvent(messageId=self._current_message_id))
            self._has_open_message = False

    def _close_all_tool_calls(self) -> None:
        """Close all open tool calls."""
        for tc_id in list(self._open_tool_calls):
            self._emit(ToolCallEndEvent(toolCallId=tc_id))
            # Synthesize an empty result so CopilotKit's renderer can still
            # flip these orphaned tool calls to "complete" rather than hanging
            # at "inProgress" forever when the turn ends abruptly.
            self._emit(
                ToolCallResultEvent(
                    messageId=f"{tc_id}-result",
                    toolCallId=tc_id,
                    content="",
                )
            )
        self._open_tool_calls.clear()

    def _emit(self, event: AguiEvent) -> None:
        """Put an event into the asyncio queue (non-blocking)."""
        if self._queue is None:
            self._log.warning("Cannot emit — no queue: %s", event.type)
            return
        try:
            self._queue.put_nowait(event)
            # Collapse streaming content logs — only log transitions
            event_name = event.type.value
            if event_name == "TEXT_MESSAGE_CONTENT":
                self._content_chunk_count += 1
            else:
                if self._content_chunk_count > 0:
                    self._log.info(
                        "emit TEXT_MESSAGE_CONTENT ×%d", self._content_chunk_count
                    )
                    self._content_chunk_count = 0
                self._log.info("emit %s", event_name)
        except asyncio.QueueFull:
            self._log.error("Event queue full, dropping: %s", event.type)

    @staticmethod
    def _serialize_tool_result(result_obj: Any) -> str:
        """Serialize a tool call result (from ACP ``raw_output``) to a string
        suitable for ``ToolCallResultEvent.content``.

        ACP's ``raw_output`` may be:
        - a plain string → returned as-is
        - a dict with structured fields (``output``, ``error``, ``metadata``) →
          JSON-serialized so the renderer can display the full payload
        - a pydantic model → ``model_dump()``
        - None → empty string
        """
        if result_obj is None:
            return ""
        if isinstance(result_obj, str):
            return result_obj
        if hasattr(result_obj, "model_dump"):
            try:
                result_obj = result_obj.model_dump()
            except Exception:
                pass
        if isinstance(result_obj, dict):
            result_dict = cast(dict[str, Any], result_obj)
            # Prefer the ``output`` field when present (opencode populates this
            # for read/glob/bash results); fall back to the full dict so the
            # renderer still shows error/metadata payloads on failure.
            if "output" in result_dict and isinstance(result_dict["output"], str):
                return result_dict["output"]
            try:
                return json.dumps(result_dict, default=str)
            except Exception:
                return str(result_dict)
        try:
            return json.dumps(result_obj, default=str)
        except Exception:
            return str(result_obj)


# ── Module-level helpers (serialization / elicitation mode introspection) ──


def _model_to_dict(obj: Any) -> Any:
    """Best-effort conversion of an SDK pydantic model (or list/dict of them)
    into a plain JSON-able dict, using the wire aliases."""
    if obj is None:
        return None
    if isinstance(obj, list):
        items = cast(list[Any], obj)
        return [_model_to_dict(item) for item in items]
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump(by_alias=True, mode="json", exclude_none=True)
        except Exception:
            try:
                return obj.model_dump(by_alias=True, exclude_none=True)
            except Exception:
                pass
    return obj


def serialize_config_options(options: Any) -> list[dict[str, Any]]:
    """Normalize a list of ``SessionConfigOptionSelect`` /
    ``SessionConfigOptionBoolean`` into the wire shape advertised to the
    AG-UI client: ``{id, name, description?, category?, currentValue, type,
    options?}``. ``_meta`` is dropped."""
    out: list[dict[str, Any]] = []
    if not options:
        return out
    opts_list = cast(list[Any], options)
    for opt in opts_list:
        raw = _model_to_dict(opt)
        if not isinstance(raw, dict):
            continue
        d: dict[str, Any] = cast(dict[str, Any], raw)
        d.pop("field_meta", None)
        d.pop("_meta", None)
        # Normalize select options: each option → {value, name, description?}.
        opts = d.get("options")
        if isinstance(opts, list):
            norm: list[dict[str, Any]] = []
            for o in cast(list[Any], opts):
                if isinstance(o, dict):
                    option_dict = cast(dict[str, Any], o)
                    option_dict.pop("field_meta", None)
                    option_dict.pop("_meta", None)
                    norm.append(option_dict)
            d["options"] = norm
        out.append(d)
    return out


def _elicitation_id_from_mode(mode: Any) -> str | None:
    """Return the elicitation id carried by URL modes; form modes have none."""
    return getattr(mode, "elicitation_id", None) or getattr(mode, "elicitationId", None)


def _elicitation_schema_from_mode(mode: Any) -> Any:
    """Return the requested ``ElicitationSchema`` for form modes, else None."""
    return getattr(mode, "requested_schema", None) or getattr(
        mode, "requestedSchema", None
    )


def _elicitation_mode_kind(mode: Any) -> str:
    """A short string describing the elicitation mode (e.g. ``"form_session"``,
    ``"url_request"``)."""
    name = type(mode).__name__
    # ElicitationFormSessionMode → form_session, etc.
    if name.startswith("Elicitation"):
        name = name[len("Elicitation") :]
    return name
