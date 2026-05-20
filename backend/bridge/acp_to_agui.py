"""AcpToAguiBridge — translates ACP notifications into AG-UI events.

This is the core of the bridge architecture. It maintains per-run state
(open message, open tool calls) and emits properly sequenced AG-UI events
into an asyncio.Queue that the SSE endpoint drains.

ACP notification structure:
    method: "session/update"
    params: { "update": { "sessionUpdate": "<kind>", ... } }

    kinds:
        - agent_message_chunk  → TEXT_MESSAGE_START/CONTENT
        - tool_call            → TOOL_CALL_START + TOOL_CALL_ARGS (+ STATE_UPDATE if approval)
        - tool_call_update     → TOOL_CALL_ARGS or STATE_UPDATE
        - turn_end             → TEXT_MESSAGE_END + TOOL_CALL_END(s) + RUN_FINISHED
        - current_mode_update  → CUSTOM agent:mode_update

    method: "session/request_permission" (this is a REQUEST, not notification)
        → STATE_UPDATE with pending approval info

    method: "_kiro.dev/*"
        → CUSTOM events
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from backend.agui.events import (
    AguiEventType,
    BaseAguiEvent,
    CustomEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    StateUpdateEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from backend.policy.tool_policy import ToolPolicyEngine

logger = logging.getLogger(__name__)


class AcpToAguiBridge:
    """Stateful translator from ACP notifications to AG-UI events.

    One bridge instance is created per task and reused across runs.
    Call ``start_run()`` to reset per-run state and connect to a new queue.

    Typical wiring::

        bridge = AcpToAguiBridge(task_id, policy_engine)
        bridge.start_run(run_id, event_queue)
        runner.on_notification = bridge.handle_notification
        runner.on_request = bridge.handle_request
    """

    def __init__(
        self,
        task_id: str,
        policy_engine: ToolPolicyEngine,
    ) -> None:
        self.task_id = task_id
        self._policy = policy_engine
        self._log = logging.LoggerAdapter(logger, {"task_id": task_id})

        # Per-run state — reset on start_run()
        self._run_id: str | None = None
        self._queue: asyncio.Queue | None = None
        self._current_message_id: str | None = None
        self._has_open_message: bool = False
        self._open_tool_calls: set[str] = set()

        # Session-level notification buffer — holds _kiro.dev/* notifications
        # that arrive before any run starts (e.g. during session init).
        # Flushed as CUSTOM events when the first run begins.
        self._pending_notifications: list[tuple[str, dict[str, Any]]] = []

        # Callback for registering pending permissions (set by TaskManager)
        self.on_pending_permission: Any = None  # Callable[[str, int|str, dict], None]

    # ── Run lifecycle ────────────────────────────────────────────────────────

    def start_run(self, run_id: str, queue: asyncio.Queue) -> None:
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
            self._emit(RunFinishedEvent(runId=self._run_id, taskId=self.task_id))
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
                )
            )
        self._run_id = None

    # ── Main entry points ────────────────────────────────────────────────────

    def handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Route an ACP notification to the appropriate handler."""
        if self._queue is None:
            # No active run — buffer session-level notifications for later
            if method.startswith("_kiro.dev/") or method == "_session/terminate":
                self._log.debug("Buffering notification (no active run): %s", method)
                self._pending_notifications.append((method, params))
            else:
                self._log.warning("Notification received but no active run: %s", method)
            return

        if method == "session/update":
            self._handle_session_update(params)
        elif method.startswith("_kiro.dev/"):
            self._handle_agent_extension(method, params)
        elif method == "_session/terminate":
            self._handle_agent_extension(method, params)
        else:
            self._log.debug("Unhandled notification method: %s", method)

    def handle_request(
        self, method: str, params: dict[str, Any], request_id: int | str
    ) -> None:
        """Handle a request FROM the agent (e.g. session/request_permission).

        This does NOT respond — the TaskManager holds the RPC ID and responds
        when the user approves/rejects via the REST endpoint.
        """
        if method == "session/request_permission":
            self._handle_request_permission(params, request_id)
        else:
            self._log.debug("Unhandled agent request: %s (id=%s)", method, request_id)

    # ── session/update dispatch ──────────────────────────────────────────────

    def _handle_session_update(self, params: dict[str, Any]) -> None:
        update = params.get("update", {})
        if not update:
            return

        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._handle_agent_message_chunk(update)
        elif kind == "tool_call":
            self._handle_tool_call(update)
        elif kind == "tool_call_update":
            self._handle_tool_call_update(update)
        elif kind == "turn_end":
            self._handle_turn_end(update)
        elif kind == "current_mode_update":
            self._emit(
                CustomEvent(
                    name="agent:mode_update",
                    value={"modeId": update.get("modeId", "")},
                )
            )
        else:
            self._log.debug("Unhandled session/update kind: %s", kind)

    # ── Message chunks ───────────────────────────────────────────────────────

    def _handle_agent_message_chunk(self, update: dict[str, Any]) -> None:
        """Translate agent_message_chunk → TEXT_MESSAGE_START/CONTENT."""
        content = update.get("content", {})
        text = content.get("text", "")
        if not text:
            return

        # Open a new message if needed
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

    # ── Tool calls ───────────────────────────────────────────────────────────

    def _handle_tool_call(self, update: dict[str, Any]) -> None:
        """Translate tool_call → TOOL_CALL_START + TOOL_CALL_ARGS.

        If the policy says approval is needed, also emit STATE_UPDATE.
        """
        # Close open text message before tool call
        self._close_open_message()

        tool_call_id = update.get("toolCallId", str(uuid.uuid4()))
        tool_name = update.get("title", update.get("toolName", "unknown"))
        raw_input = update.get("rawInput", {})
        requires_approval = update.get("requiresApproval") or update.get("requires_approval", False)

        # Extract purpose if present
        purpose = raw_input.pop("__tool_use_purpose", None)

        # Emit TOOL_CALL_START
        self._emit(
            ToolCallStartEvent(
                toolCallId=tool_call_id,
                toolCallName=tool_name,
                parentMessageId=self._current_message_id,
            )
        )
        self._open_tool_calls.add(tool_call_id)

        # Emit TOOL_CALL_ARGS with the full args as JSON
        args_json = json.dumps(raw_input)
        self._emit(
            ToolCallArgsEvent(
                toolCallId=tool_call_id,
                delta=args_json,
            )
        )

        # Check policy
        decision = self._policy.evaluate(
            tool_name, raw_input, kiro_requires=bool(requires_approval)
        )

        if decision.requires_approval:
            # Emit STATE_UPDATE with approval info
            permission_options = update.get("permissionOptions", [])
            self._emit(
                StateUpdateEvent(
                    state={
                        "approval": {
                            "pending": True,
                            "callId": tool_call_id,
                            "toolName": tool_name,
                            "summary": purpose or f"Tool call: {tool_name}",
                            "options": permission_options,
                            "category": decision.category,
                        }
                    }
                )
            )

    def _handle_tool_call_update(self, update: dict[str, Any]) -> None:
        """Translate tool_call_update → TOOL_CALL_ARGS or TOOL_CALL_END."""
        tool_call_id = update.get("toolCallId", "")
        status = update.get("status", "")
        result = update.get("result")

        if status in ("completed", "failed"):
            if tool_call_id in self._open_tool_calls:
                self._emit(
                    ToolCallEndEvent(
                        toolCallId=tool_call_id,
                        result=str(result) if result is not None else None,
                    )
                )
                self._open_tool_calls.discard(tool_call_id)
        elif result is not None:
            # Intermediate progress — emit as args delta
            self._emit(
                ToolCallArgsEvent(
                    toolCallId=tool_call_id,
                    delta=json.dumps({"_progress": result}),
                )
            )

    # ── Turn end ─────────────────────────────────────────────────────────────

    def _handle_turn_end(self, update: dict[str, Any]) -> None:
        """Translate turn_end → close open message/tools + RUN_FINISHED."""
        self.finish_run()

    # ── Permission requests ──────────────────────────────────────────────────

    def _handle_request_permission(
        self, params: dict[str, Any], request_id: int | str
    ) -> None:
        """Handle session/request_permission from the agent.

        Emits STATE_UPDATE with pending approval info. The TaskManager stores
        the RPC ID so it can respond later via the REST approval endpoint.
        """
        tool_call = params.get("toolCall", {})
        tool_call_id = str(
            tool_call.get("toolCallId")
            or params.get("toolCallId")
            or params.get("tool_call_id")
            or request_id
        )
        options = params.get("options", params.get("permissionOptions", []))
        tool_name = tool_call.get("title", tool_call.get("toolName", "unknown"))

        self._emit(
            StateUpdateEvent(
                state={
                    "approval": {
                        "pending": True,
                        "callId": tool_call_id,
                        "toolName": tool_name,
                        "summary": f"Permission required: {tool_name}",
                        "options": options,
                    }
                }
            )
        )

        # Tell TaskManager to store the pending permission
        if self.on_pending_permission:
            self.on_pending_permission(tool_call_id, request_id, params)

    # ── Agent extension notifications → CUSTOM ───────────────────────────────

    def _handle_agent_extension(self, method: str, params: dict[str, Any]) -> None:
        """Map _kiro.dev/* and _session/* notifications to CUSTOM events."""
        # Map method name to a cleaner event name
        name_map = {
            "_kiro.dev/metadata": "agent:metadata",
            "_kiro.dev/mcp/server_initialized": "agent:mcp_initialized",
            "_kiro.dev/mcp/oauth_request": "agent:mcp_oauth",
            "_kiro.dev/compaction/status": "agent:compaction",
            "_kiro.dev/clear/status": "agent:clear",
            "_kiro.dev/commands/available": "agent:commands_available",
            "_session/terminate": "agent:subagent_terminated",
        }
        event_name = name_map.get(method, f"agent:{method.replace('_kiro.dev/', '').replace('/', '_')}")

        self._emit(CustomEvent(name=event_name, value=params))

    # ── Approval resolution (called by TaskManager) ──────────────────────────

    def on_approval_resolved(self, call_id: str, approved: bool) -> None:
        """Emit STATE_UPDATE clearing the pending approval."""
        self._emit(
            StateUpdateEvent(
                state={
                    "approval": {
                        "pending": False,
                        "callId": call_id,
                        "approved": approved,
                    }
                }
            )
        )

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
        self._open_tool_calls.clear()

    def _emit(self, event: BaseAguiEvent) -> None:
        """Put an event into the asyncio queue (non-blocking)."""
        if self._queue is None:
            self._log.warning("Cannot emit — no queue: %s", event.type)
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._log.error("Event queue full, dropping: %s", event.type)
