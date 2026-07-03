"""FakeAcpAgent — a scriptable ACP agent for integration tests.

Implements the ``acp.Agent`` Protocol. Instead of doing real work, it
replays a *script* of ``session_update`` notifications and
``request_permission`` calls that the test author programs, and records
every protocol call it receives so tests can assert on what the bridge
sent.

The fake is driven by a list of ``ScriptStep`` objects. A step is one of:

  - ``text("...")``           — emit an ``AgentMessageChunk`` text delta
  - ``tool_start(...)``       — emit ``ToolCallStart`` then ``ToolCallArgs``
  - ``tool_progress(...)``    — emit ``ToolCallProgress`` (status update / output)
  - ``tool_end(...)``         — emit ``ToolCallProgress`` with completed status
  - ``request_permission(...)``— call ``conn.request_permission`` and await the
                                 bridge's reply (this is the suspend point)
  - ``ext_notification(...)`` — send a ``_kiro.dev/*`` or other extension
                                 notification
  - ``end_turn(...)``         — return from ``prompt`` with a given stop reason

The agent runs ``prompt`` by iterating the script, emitting each step via
the ``AgentSideConnection`` (real JSON-RPC over the in-process transport),
so the bridge's full ``acp.Client`` callback path —
``session_update`` / ``request_permission`` / ``ext_notification`` — is
exercised identically to a real agent.

Why not just mock ``acp.Client`` callbacks directly on the bridge? Because
the bridge is an ``acp.Client``; the SDK dispatches incoming
``session/update`` notifications to ``bridge.session_update`` via the
``ClientSideConnection``'s router. Driving the bridge through the real
``AgentSideConnection`` → transport → ``ClientSideConnection`` path is what
makes these *integration* tests of the translation layer rather than unit
tests of the bridge class in isolation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Union

import acp
import acp.schema as schema

from tests.transport import TransportPair

logger = logging.getLogger(__name__)

__all__ = ["FakeAcpAgent", "ScriptStep", "Script"]


# ── Script step types ──────────────────────────────────────────────────────

@dataclass
class TextStep:
    """Emit a single agent text delta."""
    text: str

@dataclass
class ToolStartStep:
    """Emit ``ToolCallStart`` + ``ToolCallArgs`` for a new tool call."""
    tool_call_id: str
    title: str = "tool"
    kind: str | None = None
    raw_input: dict[str, Any] | None = None
    locations: list[dict[str, Any]] | None = None

@dataclass
class ToolProgressStep:
    """Emit ``ToolCallProgress`` — status/output update, no terminal status."""
    tool_call_id: str
    status: str | None = None
    raw_output: Any = None

@dataclass
class ToolEndStep:
    """Emit ``ToolCallProgress`` with completed/failed status + raw_output."""
    tool_call_id: str
    status: str = "completed"
    raw_output: Any = None

@dataclass
class RequestPermissionStep:
    """Fire ``conn.request_permission`` and await the bridge's response.

    This is the suspend point that maps to an AG-UI interrupt. ``await``s
    the future, so the prompt task parks here exactly as a real agent
    would (the ACP prompt is one blocking call with a mid-turn callback).
    """
    tool_call_id: str
    title: str = "needs approval"
    options: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "always", "name": "Allow always", "kind": "allow_always"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
        ]
    )

@dataclass
class ExtNotificationStep:
    """Send a vendor-extension notification (e.g. ``_kiro.dev/metadata``)."""
    method: str
    params: dict[str, Any] = field(default_factory=dict)

@dataclass
class EndTurnStep:
    """Return from ``prompt`` with the given stop reason."""
    stop_reason: str = "end_turn"

@dataclass
class SleepStep:
    """Await ``asyncio.sleep(seconds)`` — used to test timing/races."""
    seconds: float

ScriptStep = Union[
    TextStep,
    ToolStartStep,
    ToolProgressStep,
    ToolEndStep,
    RequestPermissionStep,
    ExtNotificationStep,
    EndTurnStep,
    SleepStep,
]
Script = list[ScriptStep]


# ── Convenience constructors (so tests read like a script) ────────────────

def text(s: str) -> TextStep:
    return TextStep(s)

def tool_start(tid: str, title: str = "tool", **kw: Any) -> ToolStartStep:
    return ToolStartStep(tool_call_id=tid, title=title, **kw)

def tool_progress(tid: str, **kw: Any) -> ToolProgressStep:
    return ToolProgressStep(tool_call_id=tid, **kw)

def tool_end(tid: str, **kw: Any) -> ToolEndStep:
    return ToolEndStep(tool_call_id=tid, **kw)

def request_permission(tid: str, **kw: Any) -> RequestPermissionStep:
    return RequestPermissionStep(tool_call_id=tid, **kw)

def ext_notification(method: str, **params: Any) -> ExtNotificationStep:
    return ExtNotificationStep(method=method, params=dict(params))

def end_turn(stop_reason: str = "end_turn") -> EndTurnStep:
    return EndTurnStep(stop_reason=stop_reason)

def sleep(seconds: float) -> SleepStep:
    return SleepStep(seconds)


# ── The fake agent ─────────────────────────────────────────────────────────

@dataclass
class _PromptCall:
    """Record of a single prompt() invocation."""
    session_id: str
    prompt: list[Any]
    message_id: str | None

@dataclass
class _PermissionReply:
    """Record of a permission response received from the bridge."""
    tool_call_id: str
    outcome: dict[str, Any]


class FakeAcpAgent:
    """A scriptable ``acp.Agent`` implementation for integration tests.

    Construct with a ``TransportPair`` and a ``Script``; the test (or the
    fixture) then hands ``ClientSideConnection``/``AgentSideConnection``
    objects to the bridge and the agent respectively. The agent records
    every call (initialize/new_session/prompt/set_mode/set_model/cancel/
    ext_method) so tests can assert on the protocol behaviour the bridge
    drove.
    """

    def __init__(self, transport: TransportPair, script: Script | None = None) -> None:
        self._transport = transport
        self._script: Script = list(script or [])
        self.conn: acp.AgentSideConnection | None = None

        # Recorded calls
        self.initialize_calls: list[dict[str, Any]] = []
        self.new_session_calls: list[dict[str, Any]] = []
        self.load_session_calls: list[dict[str, Any]] = []
        self.prompt_calls: list[_PromptCall] = []
        self.set_mode_calls: list[tuple[str, str]] = []
        self.set_model_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[str] = []
        self.ext_method_calls: list[tuple[str, dict[str, Any]]] = []
        self.ext_notification_calls: list[tuple[str, dict[str, Any]]] = []

        # Permission replies the bridge sent back (collected as they arrive).
        self.permission_replies: list[_PermissionReply] = []

        # Per-session state we expose to the bridge's new_session response.
        self._session_id = "fake-session-1"
        self._modes: list[dict[str, Any]] | None = None
        self._models: list[dict[str, Any]] | None = None

        # The currently-running prompt task, so tests can await it.
        self._prompt_task: asyncio.Task[Any] | None = None

        # An event set when prompt() has fully returned (script consumed).
        self.prompt_done = asyncio.Event()

        # If set, prompt() raises this instead of running the script. Set
        # AFTER attach() (the router captured the bound method at attach
        # time, but it reads this attribute on `self` at call time, so a
        # late assignment still takes effect).
        self.prompt_exception: BaseException | None = None

    # ── Wiring ──────────────────────────────────────────────────────────

    def attach(self) -> acp.AgentSideConnection:
        """Build the ``AgentSideConnection`` over the agent side of the
        in-process transport. Must be called inside a running event loop.
        """
        from acp import AgentSideConnection  # deprecated but the bridge uses it
        # use_unstable_protocol=True so set_session_model (marked unstable in
        # the ACP router) is accepted — models a real agent that supports
        # models. Without it the router rejects session/set_model with
        # method_not_found before the fake's handler ever runs.
        self.conn = AgentSideConnection(
            self,
            self._transport.agent_writer,
            self._transport.agent_reader,
            use_unstable_protocol=True,
        )
        return self.conn

    async def aclose(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # ── acp.Agent Protocol ──────────────────────────────────────────────

    def on_connect(self, conn: Any) -> None:
        # SDK calls this; nothing to do for the fake.
        pass

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any | None = None,
        client_info: Any | None = None,
        **kwargs: Any,
    ) -> schema.InitializeResponse:
        self.initialize_calls.append({
            "protocol_version": protocol_version,
            "client_capabilities": client_capabilities,
            "client_info": client_info,
            "kwargs": kwargs,
        })
        return schema.InitializeResponse(
            protocol_version=protocol_version,
            agent_info=schema.Implementation(name="fake-acp", version="0.1.0"),
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        self.new_session_calls.append({
            "cwd": cwd,
            "additional_directories": additional_directories,
            "mcp_servers": mcp_servers,
            "kwargs": kwargs,
        })
        resp_kwargs: dict[str, Any] = {"session_id": self._session_id}
        if self._modes is not None:
            resp_kwargs["modes"] = schema.SessionModeState(
                available_modes=[schema.SessionMode(id=m["id"], name=m["name"]) for m in self._modes],
                current_mode_id=self._modes[0]["id"] if self._modes else None,
            )
        return schema.NewSessionResponse(**resp_kwargs)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.LoadSessionResponse:
        self.load_session_calls.append({
            "cwd": cwd,
            "session_id": session_id,
            "additional_directories": additional_directories,
            "mcp_servers": mcp_servers,
            "kwargs": kwargs,
        })
        return schema.LoadSessionResponse()

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> schema.SetSessionModeResponse:
        self.set_mode_calls.append((session_id, mode_id))
        return schema.SetSessionModeResponse()

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> schema.SetSessionModelResponse:
        self.set_model_calls.append((session_id, model_id))
        return schema.SetSessionModelResponse()

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> schema.PromptResponse:
        rec = _PromptCall(session_id=session_id, prompt=list(prompt), message_id=message_id)
        self.prompt_calls.append(rec)
        if self.prompt_exception is not None:
            exc = self.prompt_exception
            self.prompt_done.set()
            raise exc
        try:
            stop_reason = await self._run_script(session_id)
        except asyncio.CancelledError:
            stop_reason = "cancelled"
            raise
        finally:
            self.prompt_done.set()
        return schema.PromptResponse(stop_reason=stop_reason)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        self.cancel_calls.append(session_id)

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.ext_method_calls.append((method, params))
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        self.ext_notification_calls.append((method, params))

    # Other Agent methods the router may route — provide minimal stubs so
    # an unexpected client request doesn't crash the test; they record too.
    async def list_sessions(self, **kwargs: Any) -> schema.ListSessionsResponse:
        return schema.ListSessionsResponse(sessions=[])
    async def close_session(self, session_id: str, **kwargs: Any) -> schema.CloseSessionResponse:
        return schema.CloseSessionResponse()
    async def fork_session(self, **kwargs: Any) -> schema.ForkSessionResponse:
        return schema.ForkSessionResponse(session_id=str(uuid.uuid4()))
    async def resume_session(self, **kwargs: Any) -> schema.ResumeSessionResponse:
        return schema.ResumeSessionResponse()
    async def authenticate(self, method_id: str, **kwargs: Any) -> schema.AuthenticateResponse:
        return schema.AuthenticateResponse()

    # ── Script runner ───────────────────────────────────────────────────

    async def _run_script(self, session_id: str) -> str:
        """Walk the script, emitting each step through the AgentSideConnection."""
        assert self.conn is not None, "FakeAcpAgent.attach() not called"
        stop_reason = "end_turn"
        for step in self._script:
            if isinstance(step, TextStep):
                await self.conn.session_update(
                    session_id,
                    acp.update_agent_message_text(step.text),
                )
            elif isinstance(step, ToolStartStep):
                await self.conn.session_update(
                    session_id,
                    self._build_tool_call_start(step),
                )
                # Emit an args chunk right after start, mirroring how a real
                # agent streams tool-call parameters.
                await self.conn.session_update(
                    session_id,
                    self._build_tool_call_progress(
                        step.tool_call_id,
                        status="in_progress",
                        raw_input=step.raw_input,
                    ),
                )
            elif isinstance(step, ToolProgressStep):
                await self.conn.session_update(
                    session_id,
                    self._build_tool_call_progress(
                        step.tool_call_id,
                        status=step.status,
                        raw_output=step.raw_output,
                    ),
                )
            elif isinstance(step, ToolEndStep):
                await self.conn.session_update(
                    session_id,
                    self._build_tool_call_progress(
                        step.tool_call_id,
                        status=step.status,
                        raw_output=step.raw_output,
                    ),
                )
            elif isinstance(step, RequestPermissionStep):
                await self._do_request_permission(session_id, step)
            elif isinstance(step, ExtNotificationStep):
                await self.conn.ext_notification(step.method, step.params)
            elif isinstance(step, EndTurnStep):
                stop_reason = step.stop_reason
                break
            elif isinstance(step, SleepStep):
                await asyncio.sleep(step.seconds)
            else:
                raise TypeError(f"unknown script step: {step!r}")
        return stop_reason

    def _build_tool_call_start(self, step: ToolStartStep) -> schema.ToolCallStart:
        kwargs: dict[str, Any] = {
            "tool_call_id": step.tool_call_id,
            "title": step.title,
            "status": "pending",
        }
        if step.kind:
            kwargs["kind"] = step.kind
        if step.raw_input is not None:
            kwargs["raw_input"] = step.raw_input
        if step.locations:
            kwargs["locations"] = [schema.ToolCallLocation(**loc) for loc in step.locations]
        return acp.start_tool_call(**kwargs)

    def _build_tool_call_progress(
        self,
        tool_call_id: str,
        *,
        status: str | None = None,
        raw_input: Any = None,
        raw_output: Any = None,
    ) -> schema.ToolCallProgress:
        return acp.update_tool_call(
            tool_call_id,
            status=status,
            raw_input=raw_input,
            raw_output=raw_output,
        )

    async def _do_request_permission(self, session_id: str, step: RequestPermissionStep) -> None:
        assert self.conn is not None
        options = [schema.PermissionOption(**opt) for opt in step.options]
        # The tool_call passed to request_permission is a ToolCallUpdate —
        # build one describing the call being approved.
        tool_call = schema.ToolCallUpdate(
            tool_call_id=step.tool_call_id,
            title=step.title,
        )
        resp = await self.conn.request_permission(
            options=options,
            session_id=session_id,
            tool_call=tool_call,
        )
        raw_outcome = getattr(resp, "outcome", resp)
        # Serialize the pydantic AllowedOutcome/DeniedOutcome to a plain dict
        # so tests can do ``reply.outcome["outcome"]`` without import fuss.
        if hasattr(raw_outcome, "model_dump"):
            outcome_dict = raw_outcome.model_dump(by_alias=True, mode="json")
        elif isinstance(raw_outcome, dict):
            outcome_dict = raw_outcome
        else:
            outcome_dict = {"outcome": str(raw_outcome)}
        self.permission_replies.append(
            _PermissionReply(
                tool_call_id=step.tool_call_id,
                outcome=outcome_dict,
            )
        )
