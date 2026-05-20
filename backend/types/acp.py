"""ACP (Agent Communication Protocol) type definitions.

JSON-RPC 2.0 based protocol for communicating with ACP-compatible coding agents.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int
    method: str
    params: dict[str, Any] | None = None


class JsonRpcResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: int
    result: Any | None = None
    error: dict[str, Any] | None = None


class JsonRpcNotification(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: dict[str, Any] | None = None


class ClientCapabilities(BaseModel):
    fs: dict[str, bool] | None = Field(
        default_factory=lambda: {"readTextFile": True, "writeTextFile": True}
    )
    terminal: bool = True


class InitializeParams(BaseModel):
    protocolVersion: int = 1
    clientCapabilities: ClientCapabilities = Field(default_factory=ClientCapabilities)
    clientInfo: dict[str, str] = Field(
        default_factory=lambda: {"name": "acp-to-agui", "version": "0.1.0"}
    )


class AgentCapabilities(BaseModel):
    loadSession: bool | None = None
    promptCapabilities: dict[str, Any] | None = None


class InitializeResult(BaseModel):
    protocolVersion: int
    agentCapabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    agentInfo: dict[str, Any] = Field(default_factory=dict)


class SessionNewParams(BaseModel):
    cwd: str
    mcpServers: list[dict[str, Any]] = Field(default_factory=list)


class ModeInfo(BaseModel):
    id: str
    name: str
    description: str | None = None


class ModesInfo(BaseModel):
    currentModeId: str
    availableModes: list[ModeInfo] = Field(default_factory=list)


class SessionNewResult(BaseModel):
    sessionId: str
    modes: ModesInfo | None = None


class PromptContent(BaseModel):
    type: Literal["text", "image"]
    text: str | None = None
    data: str | None = None
    mimeType: str | None = None


class SessionPromptParams(BaseModel):
    sessionId: str
    prompt: list[PromptContent]


class SessionCancelParams(BaseModel):
    sessionId: str


class SessionSetModelParams(BaseModel):
    sessionId: str
    modelId: str


class SessionSetModeParams(BaseModel):
    sessionId: str
    modeId: str


class SessionCommandParams(BaseModel):
    sessionId: str
    command: str
    args: str | None = None


class PermissionOption(BaseModel):
    optionId: str
    name: str
    kind: Literal["allow_once", "allow_always", "reject_once", "reject_always"]
