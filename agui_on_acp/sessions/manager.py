"""SessionManager — orchestrates session lifecycle using the official ACP SDK.

Manages active sessions: spawns an agent via acp.spawn_agent_process,
initialises the connection, holds per-session state, and coordinates
run/approval flows.

The three entry points are the PLAN3 Create / Connect / Prompt split:

  * ``create_session``  — spawn subprocess, ``initialize``, ``session/new``
  * ``connect_session`` — spawn subprocess, ``initialize``, ``session/load``
    (replays history as a ``MESSAGES_SNAPSHOT``)
  * ``attach_for_prompt`` — reuse a live ``ActiveSession`` if present, else
    spawn subprocess, ``initialize``, ``session/resume``. NEVER falls back
    to ``session/new`` or ``session/load``.

A conversation's id never changes silently: the AG-UI ``threadId`` IS the
ACP ``session_id`` (``ActiveSession.session_id``). The old two-id split
(``task_id`` vs ``agent_session_id``) is gone.

All session state is in-memory except one durable fact: the ``cwd`` each
``session_id`` belongs to (``SessionStore``), written at ``create_session``
time so ``connect``/``attach`` can answer "what cwd does this id belong to?"
without the client resending it. Conversation content and run state stay
in the ACP agent's own backing store; the bridge process restart drops
in-memory state, and an AG-UI client resumes its ``threadId`` against a
fresh ``attach_for_prompt`` call — the store record (plus the agent's own
persistence) is what gives the id continuity.
"""

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, cast

import acp

from agui_on_acp.agent.acp_protocol import AcpProtocol
from agui_on_acp.agent.runner import AgentRunner
from agui_on_acp.agui.events import AguiEvent
from agui_on_acp.bridge.acp_to_agui import AcpToAguiBridge, serialize_config_options
from agui_on_acp.config import data_dir as _data_dir_config
from agui_on_acp.sessions.store import SessionStore

logger = logging.getLogger(__name__)


# ── Exceptions ─────────────────────────────────────────────────────────────


class SessionManagerError(Exception):
    """Base class for SessionManager-raised errors."""


class LoadSessionUnsupportedError(SessionManagerError):
    """The agent does not advertise ``loadSession`` (connect unsupported)."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session/load not supported (session={session_id})")
        self.session_id = session_id


class SessionNotFoundError(SessionManagerError):
    """The ACP agent returned ``-32002 resource_not_found`` for the id."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session not found: {session_id}")
        self.session_id = session_id


class CwdRecordNotFoundError(SessionManagerError):
    """The bridge has no durable ``session_id → cwd`` record for the id.

    Distinct from ``SessionNotFoundError`` (the ACP agent itself has no
    session by that id). This fires when the bridge was never told the
    cwd — typically a session created before the cwd-persistence store
    shipped, or by something other than this bridge. The endpoint surfaces
    it as a 404 with a message that points at the bridge's own state, not
    at the agent, so the user isn't sent looking for a missing session in
    the agent's store.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"agui-on-acp has no cwd record for session {session_id} "
            "(it was likely created before session-cwd persistence was "
            "introduced, or by something other than this bridge). "
            "Create a new session via POST /ag-ui/sessions to continue."
        )
        self.session_id = session_id


class ResumeUnsupportedError(SessionManagerError):
    """The agent does not advertise ``sessionCapabilities.resume``."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session/resume not supported (session={session_id})")
        self.session_id = session_id


class SessionResumeFailedError(SessionManagerError):
    """``session/resume`` itself failed (e.g. dead/unknown id)."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session/resume failed: {session_id}")
        self.session_id = session_id


class ListUnsupportedError(SessionManagerError):
    """The agent does not advertise ``sessionCapabilities.list``."""


class DeleteUnsupportedError(SessionManagerError):
    """The agent does not advertise ``sessionCapabilities.delete``."""


# ── ActiveSession ──────────────────────────────────────────────────────────


@dataclass
class ActiveSession:
    """One live conversation.

    ``session_id`` is the single id for the conversation: it is both the
    AG-UI ``threadId`` and the ACP ``session_id`` (the old separate
    ``agent_session_id`` field is gone — collapsing the two ids is the
    point of this layer).
    """

    session_id: str
    cwd: str
    runner: AgentRunner
    protocol: AcpProtocol
    bridge: AcpToAguiBridge
    event_queues: dict[str, asyncio.Queue[AguiEvent]] = field(
        default_factory=dict[str, asyncio.Queue[AguiEvent]]
    )
    current_run_id: str | None = None
    modes: list[dict[str, str]] | None = None
    models: list[dict[str, str]] | None = None
    current_mode_id: str | None = None
    # ACP 0.11 config options (carries the model list + arbitrary config).
    # Stored as the wire-shaped dicts produced by the bridge serializer so
    # the STATE_SNAPSHOT emitter can pass them straight through.
    config_options: list[dict[str, Any]] | None = None
    last_active_at: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        """Mark the session as recently active (idle-TTL reaper)."""
        self.last_active_at = time.monotonic()


# ── SessionManager ─────────────────────────────────────────────────────────


class SessionManager:
    """Orchestrates ACP session lifecycle (create / connect / prompt / resume).

    Holds an in-memory ``session_id → ActiveSession`` table and coordinates
    subprocess spawn, protocol initialisation, and run/permission flows.
    """

    def __init__(
        self,
        agent_command: list[str] | None = None,
        *,
        data_dir: str | None = None,
    ) -> None:
        self._sessions: dict[str, ActiveSession] = {}
        self._agent_command = agent_command or ["opencode", "acp"]
        self._capabilities: acp.schema.AgentCapabilities | None = None
        self._capabilities_lock = asyncio.Lock()
        # Persistent ``session_id → cwd`` record. Written at create_session,
        # read by connect/attach (so a client resuming by threadId alone
        # doesn't need to resend cwd), removed on delete_session. ``data_dir``
        # defaults to the configured base; tests inject a temp dir.
        base = Path(data_dir) if data_dir is not None else Path(_data_dir_config())
        self._store = SessionStore(base)

    @property
    def sessions(self) -> dict[str, ActiveSession]:
        """Read-only view of the active sessions (used by the AG-UI router)."""
        return self._sessions

    # ── Capability cache ──────────────────────────────────────────────────

    async def get_capabilities(self) -> acp.schema.AgentCapabilities:
        """Return cached ``agentCapabilities``, probing a throwaway subprocess
        on first call if no real session has cached them yet."""
        if self._capabilities is not None:
            return self._capabilities
        async with self._capabilities_lock:
            if self._capabilities is not None:  # another task won the race
                return self._capabilities
            probe_bridge = AcpToAguiBridge("capability-probe")
            runner = AgentRunner("capability-probe", command=self._agent_command)
            try:
                conn = await runner.spawn(client=probe_bridge)
                protocol = AcpProtocol("capability-probe")
                protocol.conn = conn
                result = await protocol.initialize()
                self._capabilities = result.agent_capabilities
            finally:
                await runner.kill()
            # Empty AgentCapabilities() when the agent advertised nothing.
            if self._capabilities is None:
                self._capabilities = acp.schema.AgentCapabilities()
            return self._capabilities

    async def _probe_call(self, fn: Callable[[AcpProtocol], Awaitable[Any]]) -> Any:
        """Spawn a short-lived subprocess, call ``fn(protocol)`` on it, kill.

        Used by ``list_sessions`` / ``delete_session`` when no live
        ``ActiveSession`` owns the target — mirrors ``get_capabilities``'
        probe pattern, factored out for reuse.
        """
        probe_bridge = AcpToAguiBridge("probe")
        runner = AgentRunner("probe", command=self._agent_command)
        try:
            conn = await runner.spawn(client=probe_bridge)
            protocol = AcpProtocol("probe")
            protocol.conn = conn
            await protocol.initialize()
            return await fn(protocol)
        finally:
            await runner.kill()

    # ── Create / Connect / Attach ──────────────────────────────────────────

    async def create_session(
        self,
        cwd: str,
        mode: str | None = None,
        model: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
        config_options: dict[str, Any] | None = None,
    ) -> ActiveSession:
        """Create a fresh session: ``session/new``."""
        bridge = AcpToAguiBridge("<pending>")
        bridge.cwd = cwd
        runner = AgentRunner("<pending>", command=self._agent_command)
        conn = await runner.spawn(client=bridge)
        protocol = AcpProtocol("<pending>")
        protocol.conn = conn

        init_result = await protocol.initialize()
        self._capabilities = (
            init_result.agent_capabilities or acp.schema.AgentCapabilities()
        )

        mcp_list = _normalize_mcp_servers(mcp_servers)
        result = await protocol.new_session(cwd, mcp_list)
        session_id = str(result.session_id)

        # Relabel the placeholders with the real id now that we know it.
        bridge.task_id = session_id
        runner.task_id = session_id

        modes, models, current_mode_id, config_opts = _extract_session_meta(result)
        await _apply_session_options(protocol, session_id, mode, model, config_options)
        if mode and mode != "default":
            current_mode_id = mode

        active = ActiveSession(
            session_id=session_id,
            cwd=cwd,
            runner=runner,
            protocol=protocol,
            bridge=bridge,
            modes=modes,
            models=models,
            current_mode_id=current_mode_id,
            config_options=config_opts,
        )
        self._sessions[session_id] = active
        # Persist the ``session_id → cwd`` record so connect/attach can
        # resolve cwd without the client resending it.
        await self._store.put(session_id, cwd)
        logger.info("session ready → %s", session_id)
        return active

    async def resolve_cwd(self, session_id: str) -> str:
        """Return the cwd the bridge recorded for ``session_id``.

        Prefers a live ``ActiveSession.cwd`` (always current); falls back
        to the durable ``SessionStore`` record. Raises
        ``CwdRecordNotFoundError`` if neither has a record — i.e. the bridge
        never created this id (and wasn't told about it by a prior
        process's store). Callers use this to stop requiring the client to
        resend ``cwd`` on connect/prompt.
        """
        active = self._sessions.get(session_id)
        if active is not None:
            return active.cwd
        cwd = await self._store.get(session_id)
        if cwd is None:
            raise CwdRecordNotFoundError(session_id)
        return cwd

    async def connect_session(
        self,
        session_id: str,
        cwd: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
    ) -> tuple[ActiveSession, asyncio.Queue[AguiEvent]]:
        """Connect to (replay) an existing session: ``session/load``.

        ``cwd`` is resolved from the live session or the durable store when
        not supplied — the client no longer needs to resend it. Returns the
        new ``ActiveSession`` plus the replay queue the bridge filled during
        ``session/load`` (the endpoint streams the latter as the SSE body).
        """
        caps = await self.get_capabilities()
        if not caps.load_session:
            raise LoadSessionUnsupportedError(session_id)

        # Resolve cwd from the live session / durable store when the caller
        # didn't supply it — the AG-UI client no longer needs to resend cwd
        # on connect. A caller-supplied cwd is ignored (the stored record is
        # authoritative; letting a client override it would reintroduce the
        # correctness gap this store exists to close).
        if cwd is None:
            cwd = await self.resolve_cwd(session_id)

        # If a live ActiveSession already owns this id (e.g. the client
        # ``POST /ag-ui/sessions`` created it moments ago and is now
        # ``connect``-ing to replay the initial transcript), kill its
        # subprocess BEFORE spawning a fresh one for the replay. Without
        # this, the unconditional ``self._sessions[session_id] = active``
        # below would orphan the old subprocess — it stays running,
        # unreferenced, until ``sweep_idle`` eventually reaps it (up to
        # ``IDLE_TTL_SECONDS``). Re-issuing ``session/load`` on the
        # existing live connection instead is unsafe (it would duplicate
        # history into a live stream — the same reason
        # ``attach_for_prompt`` never calls ``session/load``), so a fresh
        # subprocess is unavoidable and the old one must be killed.
        existing = self._sessions.pop(session_id, None)
        if existing is not None:
            await existing.runner.kill()

        bridge = AcpToAguiBridge(session_id)
        bridge.cwd = cwd
        runner = AgentRunner(session_id, command=self._agent_command)
        conn = await runner.spawn(client=bridge)
        protocol = AcpProtocol(session_id)
        protocol.conn = conn
        init_result = await protocol.initialize()
        self._capabilities = init_result.agent_capabilities or self._capabilities

        # ORDERING FIX: attach the replay queue BEFORE calling load_session.
        # session/load delivers the replay as session/update notifications
        # arriving *during* this await, and bridge._emit() drops events when
        # self._queue is None.
        replay_queue: asyncio.Queue[AguiEvent] = asyncio.Queue()
        bridge.start_replay(replay_queue)

        try:
            await protocol.load_session(
                session_id, cwd, _normalize_mcp_servers(mcp_servers)
            )
        except acp.RequestError as exc:
            await runner.kill()
            if exc.code == -32002:  # resource_not_found
                raise SessionNotFoundError(session_id) from exc
            raise

        bridge.end_replay()

        active = ActiveSession(
            session_id=session_id,
            cwd=cwd,
            runner=runner,
            protocol=protocol,
            bridge=bridge,
        )
        self._sessions[session_id] = active
        logger.info("session connected (replayed) → %s", session_id)
        return active, replay_queue

    async def attach_for_prompt(
        self,
        session_id: str,
        cwd: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
    ) -> ActiveSession:
        """Attach to a session for a new prompt turn.

        If a live ``ActiveSession`` already exists for ``session_id``,
        return it (no ACP call). Otherwise spawn a subprocess and call
        ``session/resume`` — NEVER ``session/new`` or ``session/load``.

        ``cwd`` is resolved from the live session / durable store when not
        supplied; the client no longer needs to resend it on prompt.
        """
        existing = self._sessions.get(session_id)
        if existing is not None:
            existing.touch()
            return existing

        if cwd is None:
            cwd = await self.resolve_cwd(session_id)

        caps = await self.get_capabilities()
        sc = caps.session_capabilities
        if sc is None or not sc.resume:
            raise ResumeUnsupportedError(session_id)

        bridge = AcpToAguiBridge(session_id)
        bridge.cwd = cwd
        runner = AgentRunner(session_id, command=self._agent_command)
        conn = await runner.spawn(client=bridge)
        protocol = AcpProtocol(session_id)
        protocol.conn = conn
        init_result = await protocol.initialize()
        # Opportunistic refresh — but don't clobber a richer cached set.
        if init_caps := init_result.agent_capabilities:
            self._capabilities = init_caps

        try:
            await protocol.resume_session(
                session_id, cwd, _normalize_mcp_servers(mcp_servers)
            )
        except acp.RequestError as exc:
            await runner.kill()
            raise SessionResumeFailedError(session_id) from exc

        active = ActiveSession(
            session_id=session_id,
            cwd=cwd,
            runner=runner,
            protocol=protocol,
            bridge=bridge,
        )
        self._sessions[session_id] = active
        logger.info("session resumed for prompt → %s", session_id)
        return active

    # ── Session management endpoints ───────────────────────────────────────

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> acp.schema.ListSessionsResponse:
        """List sessions, preferring a live subprocess for the cwd."""
        caps = await self.get_capabilities()
        sc = caps.session_capabilities
        if sc is None or not sc.list:
            raise ListUnsupportedError()
        # Prefer a live subprocess for this cwd if one exists (avoids a spawn)
        active = (
            next((a for a in self._sessions.values() if a.cwd == cwd), None)
            if cwd
            else None
        )
        if active is not None:
            return await active.protocol.list_sessions(cwd=cwd, cursor=cursor)
        return await self._probe_call(lambda p: p.list_sessions(cwd=cwd, cursor=cursor))

    async def delete_session(self, session_id: str) -> None:
        """Delete a session from the agent and drop the bridge's cwd record."""
        caps = await self.get_capabilities()
        sc = caps.session_capabilities
        if sc is None or not sc.delete:
            raise DeleteUnsupportedError()
        active = self._sessions.pop(session_id, None)
        if active is not None:
            # Kill the live subprocess's resources first, THEN delete the
            # persisted record — mirrors opencode's close-before-delete
            # rationale (opencode_acp_extensions.md Extension C).
            try:
                await active.protocol.delete_session(session_id)
            finally:
                await active.runner.kill()
        else:
            await self._probe_call(lambda p: p.delete_session(session_id))
        # Drop the bridge's own cwd record so it doesn't accumulate rows
        # for sessions that no longer exist.
        await self._store.remove(session_id)

    # ── Idle TTL reaper ───────────────────────────────────────────────────

    async def sweep_idle(self, ttl_seconds: float) -> list[str]:
        """Destroy every ``ActiveSession`` untouched for > ``ttl_seconds``.

        Sessions with a pending permission/elicitation interrupt are exempt
        — a user mid-approval-dialog shouldn't have their subprocess killed
        by an unrelated timer. Returns the ids destroyed, for logging/testing.
        """
        now = time.monotonic()
        stale: list[str] = []
        for sid, a in list(self._sessions.items()):
            if now - a.last_active_at <= ttl_seconds:
                continue
            if a.bridge.pending_interrupt_ids():
                # Exempt — a suspended interrupt counts as activity.
                continue
            stale.append(sid)
        for sid in stale:
            await self.destroy(sid)
        return stale

    # ── Run lifecycle (unchanged shape, session_id instead of agent_session_id)

    async def start_run(
        self,
        task_id: str,
        input_data: dict[str, Any],
    ) -> str:
        """Start a new run on ``task_id``: build the prompt, emit RUN_STARTED.

        Returns the generated ``run_id``. The prompt task runs in the
        background and emits AG-UI events into the run's queue.
        """
        active = self._get_active(task_id)
        active.touch()
        run_id = str(uuid.uuid4())

        queue: asyncio.Queue[AguiEvent] = asyncio.Queue()
        active.event_queues[run_id] = queue
        active.current_run_id = run_id

        messages = input_data.get("messages", [])
        text = ""
        attachments: list[dict[str, Any]] = []
        if messages:
            last = messages[-1]
            text = last.get("content", "")
            attachments = last.get("attachments", [])

        prompt: list[dict[str, Any]] = []
        if text:
            prompt.append({"type": "text", "text": text})

        for att in attachments:
            att_type = att.get("type", "file")
            att_name = att.get("name", "unnamed")
            att_mime = att.get("mimeType", "application/octet-stream")
            att_data = att.get("data", "")
            if att_type == "image":
                prompt.append({"type": "image", "data": att_data, "mimeType": att_mime})
            else:
                try:
                    decoded = base64.b64decode(att_data).decode(
                        "utf-8", errors="replace"
                    )
                    prompt.append(
                        {
                            "type": "text",
                            "text": f"[File: {att_name}]\n```\n{decoded}\n```",
                        }
                    )
                except (ValueError, UnicodeDecodeError):
                    # base64 decode or utf-8 decode failed — surface a
                    # readable placeholder instead of crashing the run.
                    prompt.append(
                        {
                            "type": "text",
                            "text": f"[File: {att_name} — could not decode]",
                        }
                    )

        if not prompt:
            prompt.append({"type": "text", "text": ""})

        active.bridge.start_run(run_id, queue)

        asyncio.create_task(self._run_prompt(active, run_id, prompt))
        return run_id

    async def _run_prompt(
        self, active: ActiveSession, run_id: str, prompt: list[dict[str, Any]]
    ) -> None:
        """Background task: send the prompt and emit RUN_FINISHED/ RUN_ERROR."""
        queue = active.event_queues.get(run_id)
        if queue is None:
            return
        try:
            await active.protocol.prompt(active.session_id, prompt)
            if active.bridge.run_id is not None:
                active.bridge.finish_run()
        except Exception:  # pylint: disable=broad-exception-caught
            logger.exception("Run %s failed", run_id)
            active.bridge.error_run(f"run {run_id} failed")

    def get_event_queue(
        self, task_id: str, run_id: str
    ) -> asyncio.Queue[AguiEvent] | None:
        """Return the event queue for a run, or None if not found."""
        active = self._sessions.get(task_id)
        if active is None:
            return None
        return active.event_queues.get(run_id)

    async def resume_run(
        self, task_id: str, resume_entries: list[dict[str, Any]]
    ) -> str:
        """Resume a prompt task suspended at a permission interrupt.

        Creates a new queue + run id, re-attaches the bridge sink to it (emits
        RUN_STARTED without resetting tool-call state), then resolves the
        parked permission Future(s) from the resume entries. The prompt task
        wakes from ``await future`` and continues emitting into the new queue.

        Returns the new run_id. Raises ``KeyError`` if the session is unknown
        or ``ValueError`` if there are no pending interrupts to resume.
        """
        active = self._get_active(task_id)
        active.touch()
        run_id = str(uuid.uuid4())

        queue: asyncio.Queue[AguiEvent] = asyncio.Queue()
        active.event_queues[run_id] = queue
        active.current_run_id = run_id

        pending = active.bridge.pending_interrupt_ids()
        if not pending:
            raise ValueError(f"No pending interrupts for task {task_id}")

        # Re-attach the stream BEFORE resolving futures so events emitted by
        # the woken prompt task land in the new queue, not the nulled one.
        active.bridge.attach_resume_queue(run_id, queue)

        # Resolve each resume entry against its parked future. The bridge
        # dispatches by id across both the permission and elicitation tables.
        for entry in resume_entries:
            interrupt_id = str(entry.get("interruptId", ""))
            status = str(entry.get("status", ""))
            payload = entry.get("payload")
            active.bridge.resolve_interrupt(interrupt_id, status, payload)

        return run_id

    async def cancel_run(self, task_id: str) -> None:
        """Cancel a run: resolve parked futures as cancelled, send ``session/cancel``."""
        active = self._get_active(task_id)
        # Resolve any parked permission futures as cancelled so
        # request_permission unblocks and the prompt task unwinds instead of
        # hanging on a dead session.
        active.bridge.cancel_all_permissions()
        await active.protocol.cancel(active.session_id)

    async def set_mode(self, task_id: str, mode_id: str) -> Any:
        """Set the agent's mode for a session."""
        active = self._get_active(task_id)
        result = await active.protocol.set_mode(active.session_id, mode_id)
        active.current_mode_id = mode_id
        return result

    async def set_model(self, task_id: str, model_id: str) -> None:
        """Set the agent's model for a session."""
        active = self._get_active(task_id)
        await active.protocol.set_model(active.session_id, model_id)

    async def set_config_option(self, task_id: str, config_id: str, value: Any) -> None:
        """Apply a single config option mid-session via
        ``session/set_config_option`` (ACP 0.11)."""
        active = self._get_active(task_id)
        await active.protocol.set_config_option(active.session_id, config_id, value)

    async def execute_command(
        self, task_id: str, command: str, args: dict[str, Any] | None = None
    ) -> None:
        """Send a ``session/command`` extension call to the agent."""
        active = self._get_active(task_id)
        args_str = args.get("args", "") if args else ""
        await active.protocol.execute_command(active.session_id, command, args_str)

    async def stop(self, task_id: str) -> bool:
        """Kill a session's subprocess; return False if it was already gone."""
        active = self._sessions.pop(task_id, None)
        if active:
            await active.runner.kill()
            return True
        return False

    async def destroy(self, task_id: str) -> None:
        """Remove and kill a session (no-op if already gone)."""
        active = self._sessions.pop(task_id, None)
        if active:
            await active.runner.kill()

    async def shutdown(self) -> None:
        """Destroy all active sessions (used on app shutdown)."""
        await asyncio.gather(
            *(self.destroy(tid) for tid in list(self._sessions.keys())),
            return_exceptions=True,
        )

    def _get_active(self, task_id: str) -> ActiveSession:
        """Return the live ``ActiveSession`` for ``task_id`` or raise ``KeyError``."""
        active = self._sessions.get(task_id)
        if active is None:
            raise KeyError(f"No active session: {task_id}")
        return active


# Backward-compatible alias
TaskManager = SessionManager


# ── Helpers ────────────────────────────────────────────────────────────────


def _extract_session_meta(
    result: Any,
) -> tuple[
    list[dict[str, str]] | None,
    list[dict[str, str]] | None,
    str | None,
    list[dict[str, Any]] | None,
]:
    """Pull modes/models/config_options out of a typed NewSessionResponse /
    LoadSessionResponse / ResumeSessionResponse."""
    modes: list[dict[str, str]] | None = None
    models: list[dict[str, str]] | None = None
    current_mode_id: str | None = None

    result_modes = getattr(result, "modes", None)
    if result_modes:
        available: list[Any] = list(
            getattr(result_modes, "available_modes", None) or []
        )
        modes = [
            {"id": str(getattr(m, "id", "")), "name": str(getattr(m, "name", ""))}
            for m in available
        ]
        current_mode_id = getattr(result_modes, "current_mode_id", None) or getattr(
            result_modes, "currentModeId", None
        )
    config_opts = _normalize_config_options(getattr(result, "config_options", None))
    return modes, models, current_mode_id, config_opts


async def _apply_session_options(
    protocol: AcpProtocol,
    session_id: str,
    mode: str | None,
    model: str | None,
    config_options: dict[str, Any] | None,
) -> None:
    """Apply mode/model/config_options after a session is created/resumed.

    Each set_* call is best-effort — a failure (unsupported mode, invalid
    value) is non-fatal so the broad ``except Exception`` is intentional;
    ``exc_info=True`` ensures the full trace is logged without aborting the
    remaining options.
    """
    if mode and mode != "default":
        try:
            await protocol.set_mode(session_id, mode)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to set mode %s", mode, exc_info=True)
    if model:
        try:
            await protocol.set_model(session_id, model)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.warning("Failed to set model %s", model, exc_info=True)
    if config_options:
        for config_id, value in config_options.items():
            if config_id == "model":
                continue
            try:
                await protocol.set_config_option(session_id, config_id, value)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.warning(
                    "Failed to set config option %s=%s",
                    config_id,
                    value,
                    exc_info=True,
                )


def _normalize_config_options(options: Any) -> list[dict[str, Any]] | None:
    """Normalize the ``configOptions`` field of a NewSessionResponse /
    LoadSessionResponse into the wire-shaped dicts advertised to the AG-UI
    client. Accepts either a list of SDK models or a list of raw dicts (from
    the dict response path). Returns ``None`` when no options were advertised
    so the caller can distinguish "empty list" from "absent"."""
    if not options:
        return None
    return serialize_config_options(options)


def _normalize_mcp_servers(
    mcp_servers: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Coerce the AG-UI ``forwardedProps.mcpServers`` shape into the ACP
    ``McpServer`` schema expected by ``session/new`` / ``session/load``.

    AG-UI clients (CopilotKit) pass a ``{name: {type, url?, command?, ...}}``
    dict; ACP requires each server to carry a ``name`` and (for http/sse) a
    ``headers`` list. This fills in the dict key as ``name`` and defaults
    ``headers`` to ``[]`` so the SDK's strict validation passes. Anything
    already conforming is passed through unchanged.
    """
    if not mcp_servers:
        return []
    out: list[dict[str, Any]] = []
    for key, server in mcp_servers.items():
        if not isinstance(server, dict):
            continue
        norm: dict[str, Any] = dict(cast(dict[str, Any], server))
        norm.setdefault("name", key)
        # http/sse servers require a headers list.
        if norm.get("type") in ("http", "sse"):
            norm.setdefault("headers", [])
        out.append(norm)
    return out
