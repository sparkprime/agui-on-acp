"""AcpProtocol — thin logging wrapper over the SDK's ClientSideConnection.

Since the SDK already provides typed methods on ClientSideConnection,
this module is kept minimal. It adds structured logging and provides a
stable interface that the session manager depends on.
"""

import logging
from typing import Any

import acp

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
        if self._conn is None:
            raise RuntimeError("AcpProtocol: connection not set (agent not spawned)")
        return self._conn

    @conn.setter
    def conn(self, value: acp.ClientSideConnection) -> None:
        self._conn = value

    async def initialize(self) -> Any:
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

    async def new_session(self, cwd: str, mcp_servers: list | None = None) -> Any:
        self._log.info("new session (cwd=%s)", cwd)
        result = await self.conn.new_session(cwd=cwd, mcp_servers=mcp_servers or [])
        session_id = getattr(result, "session_id", result)
        self._log.info("session ready: %s", session_id)
        return result

    async def load_session(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> Any:
        self._log.info("Loading session %s (cwd=%s)", session_id, cwd)
        # The SDK may expose this as a method on conn; use ext_method as fallback
        try:
            result = await self.conn.ext_method(
                "session/load",
                {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers or []},
            )
        except AttributeError:
            # If ext_method doesn't exist, try direct method call
            result = await self.conn.new_session(cwd=cwd, mcp_servers=mcp_servers or [])
        return result

    async def prompt(self, session_id: str, prompt: list[dict[str, Any]]) -> Any:
        self._log.debug("Sending prompt to session %s", session_id)
        from acp.schema import ImageContentBlock, TextContentBlock

        content_blocks = []
        for item in prompt:
            if item.get("type") == "image":
                content_blocks.append(
                    ImageContentBlock(
                        type="image",
                        data=item.get("data", ""),
                        media_type=item.get("mimeType", "image/png"),
                    )
                )
            else:
                content_blocks.append(
                    TextContentBlock(type="text", text=item.get("text", ""))
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
        self._log.info("Setting mode %s for session %s", mode_id, session_id)
        return await self.conn.set_session_mode(mode_id=mode_id, session_id=session_id)

    async def set_model(self, session_id: str, model_id: str) -> Any:
        self._log.info("Setting model %s for session %s", model_id, session_id)
        return await self.conn.set_session_model(
            model_id=model_id, session_id=session_id
        )

    async def execute_command(
        self, session_id: str, command: str, args: str | None = None
    ) -> Any:
        self._log.info("Executing command /%s for session %s", command, session_id)
        name = command.lstrip("/")
        return await self.conn.ext_method(
            "session/command",
            {"sessionId": session_id, "command": {"command": name, "args": args or ""}},
        )
