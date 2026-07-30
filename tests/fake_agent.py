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
from typing import Any, Literal, Union, cast

import acp
import acp.schema as schema

from tests.transport import TransportPair

logger = logging.getLogger(__name__)

__all__ = [
    "FakeAcpAgent",
    "FakeSessionStore",
    "StoredSession",
    "ScriptStep",
    "Script",
    "capabilities",
    "text",
    "user_text",
    "tool_start",
    "tool_progress",
    "tool_end",
    "request_permission",
    "ext_notification",
    "end_turn",
    "sleep",
    "config_option_update",
    "usage",
    "session_info",
    "plan",
    "plan_removed",
    "thought",
    "elicitation",
]


# ── Backing store (shared across fake-agent instances for restart tests) ────


@dataclass
class StoredSession:
    """One persisted conversation in the fake's backing store."""

    session_id: str
    cwd: str
    transcript: list[Any] = field(default_factory=list[Any])  # Script for replay
    deleted: bool = False


class FakeSessionStore:
    """A minimal in-memory session store shared across fake-agent instances.

    Outliving any single ``FakeAcpAgent``/transport/manager triple is what
    models "the agent's persistence survived a bridge restart" — the same
    store is passed into a second ``FakeAcpAgent`` constructed after the
    first manager is discarded.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, StoredSession] = {}

    def create(self, cwd: str) -> StoredSession:
        sid = f"fake-session-{len(self.sessions) + 1}"
        s = StoredSession(session_id=sid, cwd=cwd)
        self.sessions[sid] = s
        return s

    def get(self, session_id: str) -> StoredSession:
        s = self.sessions.get(session_id)
        if s is None or s.deleted:
            raise acp.RequestError.resource_not_found(session_id)
        return s


# ── Capability helper ──────────────────────────────────────────────────────


def capabilities(
    *,
    load_session: bool = False,
    resume: bool = False,
    list_: bool = False,
    delete: bool = False,
) -> schema.AgentCapabilities:
    """Build an ``AgentCapabilities`` advertising the requested bits."""
    sc: schema.SessionCapabilities | None = None
    if resume or list_ or delete:
        sc = schema.SessionCapabilities(
            resume=schema.SessionResumeCapabilities() if resume else None,
            list=schema.SessionListCapabilities() if list_ else None,
            delete=schema.SessionDeleteCapabilities() if delete else None,
        )
    return schema.AgentCapabilities(load_session=load_session, session_capabilities=sc)


# ── Script step types ──────────────────────────────────────────────────────


@dataclass
class TextStep:
    """Emit a single text delta.

    ``role="agent"`` (default) emits an ``AgentMessageChunk``;
    ``role="user"`` emits a ``UserMessageChunk`` (used for replay scripts
    that include the user's turns).
    """

    text: str
    role: Literal["agent", "user"] = "agent"


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
    status: schema.ToolCallStatus | None = None
    raw_output: Any = None


@dataclass
class ToolEndStep:
    """Emit ``ToolCallProgress`` with completed/failed status + raw_output."""

    tool_call_id: str
    status: schema.ToolCallStatus = "completed"
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
    params: dict[str, Any] = field(default_factory=dict[str, Any])


@dataclass
class EndTurnStep:
    """Return from ``prompt`` with the given stop reason."""

    stop_reason: schema.StopReason = "end_turn"


@dataclass
class SleepStep:
    """Await ``asyncio.sleep(seconds)`` — used to test timing/races."""

    seconds: float


@dataclass
class ConfigOptionUpdateStep:
    """Emit a ``ConfigOptionUpdate`` notification (ACP 0.11)."""

    config_options: list[dict[str, Any]]


@dataclass
class UsageStep:
    """Emit a ``UsageUpdate`` notification."""

    used: int
    size: int
    cost: dict[str, Any] | None = None


@dataclass
class SessionInfoStep:
    """Emit a ``SessionInfoUpdate`` notification."""

    title: str | None = None
    updated_at: str | None = None


@dataclass
class PlanStep:
    """Emit an ``AgentPlanUpdate`` (full plan replace)."""

    entries: list[dict[str, Any]]


@dataclass
class PlanRemovedStep:
    """Emit an ``AgentPlanRemovedUpdate``."""

    plan_id: str


@dataclass
class ThoughtStep:
    """Emit an ``AgentThoughtChunk`` reasoning delta."""

    text: str


@dataclass
class ElicitationStep:
    """Fire ``conn.create_elicitation`` and await the bridge's response.

    This is the suspend point that maps to an AG-UI interrupt with
    ``reason="elicitation"``.
    """

    message: str = "Please provide input"
    mode_kind: str = "form_session"
    requested_schema: dict[str, Any] | None = None
    elicitation_id: str | None = None
    url: str | None = None


ScriptStep = Union[
    TextStep,
    ToolStartStep,
    ToolProgressStep,
    ToolEndStep,
    RequestPermissionStep,
    ExtNotificationStep,
    EndTurnStep,
    SleepStep,
    ConfigOptionUpdateStep,
    UsageStep,
    SessionInfoStep,
    PlanStep,
    PlanRemovedStep,
    ThoughtStep,
    ElicitationStep,
]
Script = list[ScriptStep]


# ── Convenience constructors (so tests read like a script) ────────────────


def text(s: str) -> TextStep:
    return TextStep(s)


def user_text(s: str) -> TextStep:
    """Emit a user-role text delta (a ``UserMessageChunk``)."""
    return TextStep(s, role="user")


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


def end_turn(stop_reason: schema.StopReason = "end_turn") -> EndTurnStep:
    return EndTurnStep(stop_reason=stop_reason)


def sleep(seconds: float) -> SleepStep:
    return SleepStep(seconds)


def config_option_update(options: list[dict[str, Any]]) -> ConfigOptionUpdateStep:
    return ConfigOptionUpdateStep(config_options=options)


def usage(used: int, size: int, **kw: Any) -> UsageStep:
    return UsageStep(used=used, size=size, **kw)


def session_info(**kw: Any) -> SessionInfoStep:
    return SessionInfoStep(**kw)


def plan(entries: list[dict[str, Any]]) -> PlanStep:
    return PlanStep(entries=entries)


def plan_removed(plan_id: str) -> PlanRemovedStep:
    return PlanRemovedStep(plan_id=plan_id)


def thought(text: str) -> ThoughtStep:
    return ThoughtStep(text=text)


def elicitation(**kw: Any) -> ElicitationStep:
    return ElicitationStep(**kw)


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


@dataclass
class _ElicitationReply:
    """Record of an elicitation response received from the bridge."""

    message: str
    action: str  # "accept" | "decline" | "cancel"
    content: dict[str, Any] | None = None


class FakeAcpAgent:
    """A scriptable ``acp.Agent`` implementation for integration tests.

    Construct with a ``TransportPair`` and a ``Script``; the test (or the
    fixture) then hands ``ClientSideConnection``/``AgentSideConnection``
    objects to the bridge and the agent respectively. The agent records
    every call (initialize/new_session/prompt/set_mode/set_model/cancel/
    ext_method) so tests can assert on the protocol behaviour the bridge
    drove.
    """

    def __init__(
        self,
        transport: TransportPair,
        script: Script | None = None,
        capabilities: schema.AgentCapabilities | None = None,
        store: FakeSessionStore | None = None,
    ) -> None:
        self.transport = transport
        self.script: Script = list(script or [])
        self.conn: acp.AgentSideConnection | None = None
        self.capabilities = capabilities
        self.store = store if store is not None else FakeSessionStore()

        # Recorded calls
        self.initialize_calls: list[dict[str, Any]] = []
        self.new_session_calls: list[dict[str, Any]] = []
        self.load_session_calls: list[dict[str, Any]] = []
        self.resume_session_calls: list[dict[str, Any]] = []
        self.close_session_calls: list[str] = []
        self.delete_session_calls: list[str] = []
        self.prompt_calls: list[_PromptCall] = []
        self.set_mode_calls: list[tuple[str, str]] = []
        self.set_model_calls: list[tuple[str, str]] = []
        self.set_config_option_calls: list[tuple[str, str, Any]] = []
        self.cancel_calls: list[str] = []
        self.ext_method_calls: list[tuple[str, dict[str, Any]]] = []
        self.ext_notification_calls: list[tuple[str, dict[str, Any]]] = []

        # Permission replies the bridge sent back (collected as they arrive).
        self.permission_replies: list[_PermissionReply] = []
        # Elicitation replies the bridge sent back.
        self.elicitation_replies: list[_ElicitationReply] = []

        # Per-session state we expose to the bridge's new_session response.
        self.modes: list[dict[str, Any]] | None = None
        self._models: list[dict[str, Any]] | None = None
        # ACP 0.11 config options advertised in new_session. Each entry is a
        # dict ready to be turned into a SessionConfigOptionSelect/Boolean.
        self.config_options: list[dict[str, Any]] | None = None

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
            self.transport.agent_writer,
            self.transport.agent_reader,
            use_unstable_protocol=True,
        )
        # The installed agent-side router (0.11.x) does not yet route
        # ``session/delete`` even though DeleteSessionRequest/Response and
        # AGENT_METHODS["session_delete"] exist. A real newer agent would
        # route it; register the route here so the fake models that. This
        # is a test-only reach-past-the-SDK; delete once the SDK grows the
        # route upstream.
        from acp.meta import AGENT_METHODS

        raw_conn = getattr(self.conn, "_conn", None)
        router = getattr(raw_conn, "_handler", None)
        if router is not None and hasattr(router, "route_request"):
            from acp.utils import normalize_result

            router.route_request(
                AGENT_METHODS["session_delete"],
                schema.DeleteSessionRequest,
                self,
                "delete_session",
                adapt_result=normalize_result,
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
        self.initialize_calls.append(
            {
                "protocol_version": protocol_version,
                "client_capabilities": client_capabilities,
                "client_info": client_info,
                "kwargs": kwargs,
            }
        )
        return schema.InitializeResponse(
            protocol_version=protocol_version,
            agent_info=schema.Implementation(name="fake-acp", version="0.1.0"),
            agent_capabilities=self.capabilities,
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.NewSessionResponse:
        self.new_session_calls.append(
            {
                "cwd": cwd,
                "additional_directories": additional_directories,
                "mcp_servers": mcp_servers,
                "kwargs": kwargs,
            }
        )
        stored = self.store.create(cwd)
        resp_kwargs: dict[str, Any] = {"session_id": stored.session_id}
        if self.modes is not None:
            modes = self.modes
            resp_kwargs["modes"] = schema.SessionModeState(
                available_modes=[
                    schema.SessionMode(id=m["id"], name=m["name"]) for m in modes
                ],
                current_mode_id=str(modes[0]["id"]) if modes else "",
            )
        if self.config_options is not None:
            resp_kwargs["config_options"] = [
                self._build_config_option(opt) for opt in self.config_options
            ]
        return schema.NewSessionResponse(**resp_kwargs)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.LoadSessionResponse:
        self.load_session_calls.append(
            {
                "cwd": cwd,
                "session_id": session_id,
                "additional_directories": additional_directories,
                "mcp_servers": mcp_servers,
                "kwargs": kwargs,
            }
        )
        stored = self.store.get(session_id)  # raises resource_not_found
        # Replay the scripted transcript as session/update notifications
        # arriving synchronously during this call — exactly what a real
        # agent's session/load delivers.
        if stored.transcript:
            await self._run_script(session_id, script=stored.transcript)
        return schema.LoadSessionResponse()

    async def set_session_mode(
        self, mode_id: str, session_id: str, **kwargs: Any
    ) -> schema.SetSessionModeResponse:
        self.set_mode_calls.append((session_id, mode_id))
        return schema.SetSessionModeResponse()

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: Any
    ) -> schema.SetSessionConfigOptionResponse:
        # In ACP 0.11+ the model is a config option with config_id == "model".
        self.set_config_option_calls.append((session_id, config_id, value))
        if config_id == "model":
            self.set_model_calls.append((session_id, str(value)))
        # Echo the advertised config options (with the new value applied) so
        # the SDK's response validation passes.
        opts = [self._build_config_option(o) for o in (self.config_options or [])]
        return schema.SetSessionConfigOptionResponse(config_options=opts)

    async def prompt(
        self,
        prompt: list[Any],
        session_id: str,
        message_id: str | None = None,
        **kwargs: Any,
    ) -> schema.PromptResponse:
        rec = _PromptCall(
            session_id=session_id, prompt=list(prompt), message_id=message_id
        )
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
        cwd = kwargs.get("cwd")
        sessions: list[schema.SessionInfo] = []
        for s in self.store.sessions.values():
            if s.deleted:
                continue
            if cwd is not None and s.cwd != cwd:
                continue
            sessions.append(schema.SessionInfo(session_id=s.session_id, cwd=s.cwd))
        return schema.ListSessionsResponse(sessions=sessions)

    async def close_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.CloseSessionResponse:
        self.close_session_calls.append(session_id)
        return schema.CloseSessionResponse()

    async def fork_session(self, **kwargs: Any) -> schema.ForkSessionResponse:
        return schema.ForkSessionResponse(session_id=str(uuid.uuid4()))

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> schema.ResumeSessionResponse:
        self.resume_session_calls.append(
            {
                "session_id": session_id,
                "cwd": cwd,
                "additional_directories": additional_directories,
                "mcp_servers": mcp_servers,
                "kwargs": kwargs,
            }
        )
        # Validate the id against the store — a missing/deleted id raises
        # resource_not_found, matching a real agent's resume failure.
        self.store.get(session_id)
        return schema.ResumeSessionResponse()

    async def delete_session(
        self, session_id: str, **kwargs: Any
    ) -> schema.DeleteSessionResponse:
        self.delete_session_calls.append(session_id)
        s = self.store.sessions.get(session_id)
        if s is not None:
            s.deleted = True
        return schema.DeleteSessionResponse()

    async def authenticate(
        self, method_id: str, **kwargs: Any
    ) -> schema.AuthenticateResponse:
        return schema.AuthenticateResponse()

    # ── Script runner ───────────────────────────────────────────────────

    async def _run_script(
        self, session_id: str, *, script: Script | None = None
    ) -> schema.StopReason:
        """Walk the script, emitting each step through the AgentSideConnection."""
        assert self.conn is not None, "FakeAcpAgent.attach() not called"
        steps = script if script is not None else self.script
        stop_reason: schema.StopReason = "end_turn"
        for step in steps:
            if isinstance(step, TextStep):
                update = (
                    acp.update_agent_message_text(step.text)
                    if step.role == "agent"
                    else acp.update_user_message_text(step.text)
                )
                await self.conn.session_update(session_id, update)
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
            elif isinstance(step, ConfigOptionUpdateStep):
                await self.conn.session_update(
                    session_id,
                    schema.ConfigOptionUpdate(
                        session_update="config_option_update",
                        config_options=[
                            self._build_config_option(o) for o in step.config_options
                        ],
                    ),
                )
            elif isinstance(step, UsageStep):
                cost_obj = None
                if step.cost is not None:
                    cost_obj = schema.Cost(
                        amount=float(step.cost.get("amount", 0.0)),
                        currency=str(step.cost.get("currency", "USD")),
                    )
                await self.conn.session_update(
                    session_id,
                    schema.UsageUpdate(
                        session_update="usage_update",
                        used=step.used,
                        size=step.size,
                        cost=cost_obj,
                    ),
                )
            elif isinstance(step, SessionInfoStep):
                await self.conn.session_update(
                    session_id,
                    schema.SessionInfoUpdate(
                        session_update="session_info_update",
                        title=step.title,
                        updated_at=step.updated_at,
                    ),
                )
            elif isinstance(step, PlanStep):
                await self.conn.session_update(
                    session_id,
                    acp.update_plan([schema.PlanEntry(**e) for e in step.entries]),
                )
            elif isinstance(step, PlanRemovedStep):
                await self.conn.session_update(
                    session_id,
                    schema.AgentPlanRemovedUpdate(
                        session_update="plan_removed", id=step.plan_id
                    ),
                )
            elif isinstance(step, ThoughtStep):
                await self.conn.session_update(
                    session_id,
                    acp.update_agent_thought_text(step.text),
                )
            elif isinstance(step, ElicitationStep):
                await self._do_elicitation(session_id, step)
            elif isinstance(step, EndTurnStep):
                stop_reason = step.stop_reason
                break
            else:
                assert isinstance(step, SleepStep), f"unknown script step: {step!r}"
                await asyncio.sleep(step.seconds)
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
            kwargs["locations"] = [
                schema.ToolCallLocation(**loc) for loc in step.locations
            ]
        return acp.start_tool_call(**kwargs)

    def _build_config_option(self, opt: dict[str, Any]) -> Any:
        """Build a ``SessionConfigOptionSelect`` or ``SessionConfigOptionBoolean``
        from a plain dict (the shape tests pass in)."""
        opt_dict = opt
        opt_type = opt_dict.get("type", "select")
        common: dict[str, Any] = {
            "id": opt_dict["id"],
            "name": opt_dict["name"],
            "description": opt_dict.get("description"),
            "category": opt_dict.get("category"),
        }
        if opt_type == "boolean":
            return schema.SessionConfigOptionBoolean(
                type="boolean",
                current_value=bool(
                    opt_dict.get("currentValue", opt_dict.get("current_value", False))
                ),
                **common,
            )
        # select
        raw_options = cast(list[dict[str, Any]], opt_dict.get("options", []))
        options: list[Any] = []
        for o in raw_options:
            if "options" in o and "group" in o:
                options.append(
                    schema.SessionConfigSelectGroup(
                        group=o["group"],
                        name=o["name"],
                        options=[
                            schema.SessionConfigSelectOption(**oo)
                            for oo in cast(list[dict[str, Any]], o["options"])
                        ],
                    )
                )
            else:
                options.append(schema.SessionConfigSelectOption(**o))
        return schema.SessionConfigOptionSelect(
            type="select",
            current_value=str(
                opt_dict.get("currentValue", opt_dict.get("current_value", ""))
            ),
            options=options,
            **common,
        )

    def _build_tool_call_progress(
        self,
        tool_call_id: str,
        *,
        status: schema.ToolCallStatus | None = None,
        raw_input: Any = None,
        raw_output: Any = None,
    ) -> schema.ToolCallProgress:
        return acp.update_tool_call(
            tool_call_id,
            status=status,
            raw_input=raw_input,
            raw_output=raw_output,
        )

    async def _do_request_permission(
        self, session_id: str, step: RequestPermissionStep
    ) -> None:
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
        raw_outcome: Any = getattr(resp, "outcome", resp)
        # Serialize the pydantic AllowedOutcome/DeniedOutcome to a plain dict
        # so tests can do ``reply.outcome["outcome"]`` without import fuss.
        outcome_dict: dict[str, Any]
        if hasattr(raw_outcome, "model_dump"):
            outcome_dict = raw_outcome.model_dump(by_alias=True, mode="json")
        elif isinstance(raw_outcome, dict):
            outcome_dict = cast(dict[str, Any], raw_outcome)
        else:
            outcome_dict = {"outcome": str(raw_outcome)}
        self.permission_replies.append(
            _PermissionReply(
                tool_call_id=step.tool_call_id,
                outcome=outcome_dict,
            )
        )

    async def _do_elicitation(self, session_id: str, step: ElicitationStep) -> None:
        assert self.conn is not None
        mode: Any
        if step.mode_kind == "url_session":
            mode = schema.ElicitationUrlSessionMode(
                session_id=session_id,
                elicitation_id=step.elicitation_id or "elic-url-1",
                url=cast(Any, step.url or "https://example.com/auth"),
            )
        elif step.mode_kind == "form_request":
            mode = schema.ElicitationFormRequestMode(
                request_id="req-1",
                requested_schema=self._build_elicitation_schema(step.requested_schema),
            )
        else:  # "form_session" (default)
            mode = schema.ElicitationFormSessionMode(
                session_id=session_id,
                requested_schema=self._build_elicitation_schema(step.requested_schema),
            )
        resp = await self.conn.create_elicitation(message=step.message, mode=mode)
        action = getattr(resp, "action", "?")
        content = getattr(resp, "content", None)
        self.elicitation_replies.append(
            _ElicitationReply(
                message=step.message,
                action=action,
                content=(
                    cast(dict[str, Any], content) if isinstance(content, dict) else None
                ),
            )
        )

    def _build_elicitation_schema(
        self, schema_dict: dict[str, Any] | None
    ) -> schema.ElicitationSchema:
        if schema_dict is None:
            return schema.ElicitationSchema(properties={})
        properties: dict[str, Any] = {}
        raw_props = cast(dict[str, dict[str, Any]], schema_dict.get("properties") or {})
        for name, prop in raw_props.items():
            prop_dict = prop
            ptype = prop_dict.get("type", "string")
            if ptype == "number":
                properties[name] = schema.ElicitationNumberPropertySchema(**prop_dict)
            elif ptype == "integer":
                properties[name] = schema.ElicitationIntegerPropertySchema(**prop_dict)
            elif ptype == "boolean":
                properties[name] = schema.ElicitationBooleanPropertySchema(**prop_dict)
            elif ptype == "array":
                properties[name] = schema.ElicitationMultiSelectPropertySchema(
                    **prop_dict
                )
            else:
                properties[name] = schema.ElicitationStringPropertySchema(**prop_dict)
        return schema.ElicitationSchema(
            title=schema_dict.get("title"),
            description=schema_dict.get("description"),
            required=schema_dict.get("required"),
            properties=properties,
        )
