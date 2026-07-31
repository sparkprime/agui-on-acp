"""AcpProtocol — thin logging wrapper over the SDK's ClientSideConnection.

Since the SDK already provides typed methods on ClientSideConnection,
this module is kept minimal. It adds structured logging and provides a
stable interface that the session manager depends on.
"""

import logging
from typing import Any

import acp
import acp.schema
from acp.meta import AGENT_METHODS
from acp.utils import request_model_from_dict

logger = logging.getLogger(__name__)


class AcpProtocol:
    """Typed ACP protocol layer over the SDK's ClientSideConnection.

    Wraps conn methods with logging. The conn is set after spawn.
    """

    def __init__(self, task_id: str) -> None:
        self._conn: acp.ClientSideConnection | None = None
        self._log = logging.LoggerAdapter(logger, {"task_id": task_id})

    @property
    def conn(self) -> acp.ClientSideConnection:
        """Return the live connection, raising if the agent hasn't spawned."""
        if self._conn is None:
            raise RuntimeError("AcpProtocol: connection not set (agent not spawned)")
        return self._conn

    @conn.setter
    def conn(self, value: acp.ClientSideConnection) -> None:
        """Set the connection (called by the runner after spawn)."""
        self._conn = value

    async def initialize(self) -> Any:
        """Send the ``initialize`` request and log the agent's identity."""
        self._log.info("initializing connection...")
        result = await self.conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_info={"name": "acp-to-agui", "version": "0.1.0"},
        )
        agent_info = getattr(result, "agent_info", None)
        name = getattr(agent_info, "title", None) or getattr(
            agent_info, "name", "unknown"
        )
        version = getattr(agent_info, "version", "?")
        self._log.info("connected → %s v%s", name, version)
        return result

    async def new_session(
        self, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> Any:
        """Send ``session/new`` and return the typed response."""
        self._log.info("new session (cwd=%s)", cwd)
        result = await self.conn.new_session(cwd=cwd, mcp_servers=mcp_servers or [])
        session_id = getattr(result, "session_id", result)
        self._log.info("session ready: %s", session_id)
        return result

    async def load_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> acp.schema.LoadSessionResponse:
        """Send ``session/load`` to replay an existing session's history."""
        self._log.info("Loading session %s (cwd=%s)", session_id, cwd)
        return await self.conn.load_session(
            session_id=session_id, cwd=cwd, mcp_servers=mcp_servers or []
        )

    async def resume_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> acp.schema.ResumeSessionResponse:
        """Send ``session/resume`` to re-attach to an existing session."""
        self._log.info("Resuming session %s (cwd=%s)", session_id, cwd)
        return await self.conn.resume_session(
            session_id=session_id, cwd=cwd, mcp_servers=mcp_servers or []
        )

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None
    ) -> acp.schema.ListSessionsResponse:
        """Send ``session/list`` (ACP 0.11)."""
        self._log.info("Listing sessions (cwd=%s, cursor=%s)", cwd, cursor)
        return await self.conn.list_sessions(cwd=cwd, cursor=cursor)

    async def delete_session(self, session_id: str) -> acp.schema.DeleteSessionResponse:
        """Send ``session/delete`` (ACP 0.11)."""
        self._log.info("Deleting session %s", session_id)
        return await _request_delete_session(self.conn, session_id)

    async def prompt(self, session_id: str, prompt: list[dict[str, Any]]) -> Any:
        """Send ``session/prompt`` with text/image content blocks."""
        self._log.debug("Sending prompt to session %s", session_id)

        content_blocks: list[Any] = []
        for item in prompt:
            if item.get("type") == "image":
                content_blocks.append(
                    acp.schema.ImageContentBlock(
                        type="image",
                        data=item.get("data", ""),
                        mime_type=item.get("mimeType", "image/png"),
                    )
                )
            else:
                content_blocks.append(
                    acp.schema.TextContentBlock(type="text", text=item.get("text", ""))
                )
        result = await self.conn.prompt(prompt=content_blocks, session_id=session_id)
        return result

    async def cancel(self, session_id: str) -> None:
        """Send ``session/cancel`` to the agent.

        This MUST be awaited: ``conn.cancel`` sends a JSON-RPC notification
        over the transport. Calling it without ``await`` (the previous
        behaviour) created the coroutine and discarded it, so the cancel
        notification was never actually written and the agent kept running.
        """
        self._log.info("Cancelling session %s", session_id)
        await self.conn.cancel(session_id=session_id)

    async def set_mode(self, session_id: str, mode_id: str) -> Any:
        """Send ``session/set_mode``."""
        self._log.info("Setting mode %s for session %s", mode_id, session_id)
        return await self.conn.set_session_mode(mode_id=mode_id, session_id=session_id)

    async def set_model(self, session_id: str, model_id: str) -> Any:
        """Set the agent's model for a session.

        In ACP 0.11+ the model is a session config option (``session/set_config_option``)
        selected via ``config_id="model"``, not its own JSON-RPC method.
        """
        self._log.info("Setting model %s for session %s", model_id, session_id)
        return await self.conn.set_config_option(
            config_id="model", session_id=session_id, value=model_id
        )

    async def set_config_option(
        self, session_id: str, config_id: str, value: Any
    ) -> Any:
        """Set an arbitrary session config option (ACP 0.11
        ``session/set_config_option``).

        ``value`` is a string for select options (e.g. ``"model"``) or a
        bool for boolean options; the SDK accepts ``str | bool``.
        """
        self._log.info(
            "Setting config option %s=%s for session %s", config_id, value, session_id
        )
        return await self.conn.set_config_option(
            config_id=config_id, session_id=session_id, value=value
        )

    async def execute_command(
        self, session_id: str, command: str, args: str | None = None
    ) -> Any:
        """Send a ``session/command`` extension method call."""
        self._log.info("Executing command /%s for session %s", command, session_id)
        name = command.lstrip("/")
        return await self.conn.ext_method(
            "session/command",
            {"sessionId": session_id, "command": {"command": name, "args": args or ""}},
        )


async def _request_delete_session(
    conn: acp.ClientSideConnection, session_id: str
) -> acp.schema.DeleteSessionResponse:
    """Send a ``session/delete`` request.

    ``acp.ClientSideConnection`` has typed wrappers for most session
    lifecycle methods (``close_session``, ``load_session``, …) but not yet
    for ``session/delete`` — even though ``DeleteSessionRequest`` /
    ``DeleteSessionResponse`` and ``AGENT_METHODS["session_delete"]``
    already exist in the SDK's schema/meta modules. This helper reaches
    past the public wrapper and calls ``request_model_from_dict`` directly
    against the connection's private ``_conn`` (a ``Connection``), mirroring
    exactly what a future upstream ``delete_session`` method would do.

    Fragile across SDK versions by design — delete once the SDK grows the
    typed method and ``AcpProtocol.delete_session`` can call it directly.
    """
    raw_conn = getattr(conn, "_conn", conn)
    return await request_model_from_dict(
        raw_conn,
        AGENT_METHODS["session_delete"],
        acp.schema.DeleteSessionRequest(session_id=session_id),
        acp.schema.DeleteSessionResponse,
    )
