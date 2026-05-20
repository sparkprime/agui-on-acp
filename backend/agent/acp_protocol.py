"""AcpProtocol — typed interface over AgentRunner.

High-level methods for each ACP operation so callers don't need to
construct raw JSON-RPC params.
"""

import logging
from typing import Any

from backend.types.acp import (
    InitializeParams,
    InitializeResult,
    PromptContent,
    SessionNewParams,
    SessionNewResult,
)
from backend.agent.runner import AgentRunner

logger = logging.getLogger(__name__)


class AcpProtocol:
    """Typed ACP protocol layer over an AgentRunner instance."""

    def __init__(self, runner: AgentRunner) -> None:
        self._runner = runner

    @property
    def runner(self) -> AgentRunner:
        return self._runner

    async def initialize(self) -> InitializeResult:
        params = InitializeParams()
        result = await self._runner.request(
            "initialize", params.model_dump(by_alias=True)
        )
        return InitializeResult.model_validate(result)

    async def session_new(
        self, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> SessionNewResult:
        params = SessionNewParams(cwd=cwd, mcpServers=mcp_servers or [])
        result = await self._runner.request(
            "session/new", params.model_dump(by_alias=True)
        )
        if isinstance(result, dict):
            models_info = result.get("models")
            if models_info:
                available = models_info.get("availableModels", [])
                current = models_info.get("currentModelId", "unknown")
                model_ids = [m.get("id", m) if isinstance(m, dict) else m for m in available]
                logger.info("Available models: %s (current: %s)", model_ids, current)
        return SessionNewResult.model_validate(result)

    async def session_load(
        self, session_id: str, cwd: str, mcp_servers: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        return await self._runner.request(
            "session/load",
            {"sessionId": session_id, "cwd": cwd, "mcpServers": mcp_servers or []},
        )

    async def session_prompt(self, session_id: str, content: list[PromptContent]) -> None:
        await self._runner.request(
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [c.model_dump(by_alias=True, exclude_none=True) for c in content],
            },
        )

    def session_cancel(self, session_id: str) -> None:
        self._runner.notify("session/cancel", {"sessionId": session_id})

    async def set_mode(self, session_id: str, mode_id: str) -> Any:
        return await self._runner.request(
            "session/set_mode", {"sessionId": session_id, "modeId": mode_id}
        )

    async def set_model(self, session_id: str, model_id: str) -> None:
        await self._runner.request(
            "session/set_model", {"sessionId": session_id, "modelId": model_id}
        )

    async def execute_command(self, session_id: str, command: str, args: str | None = None) -> None:
        name = command.lstrip("/")
        await self._runner.request(
            "session/command",
            {"sessionId": session_id, "command": {"command": name, "args": args or ""}},
        )

    def respond(self, request_id: int | str, result: Any) -> None:
        self._runner.respond(request_id, result)
