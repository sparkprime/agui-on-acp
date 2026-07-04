"""Session API request/response models."""

from typing import Any, Literal

from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    cwd: str
    title: str | None = None
    resumeSessionId: str | None = None
    mode: str | None = None
    model: str | None = None
    mcpServers: dict[str, Any] | None = None
    agentCommand: list[str] | None = None


class StartRunRequest(BaseModel):
    input: dict[str, Any]
    config: dict[str, Any] | None = None


class UpdateTaskRequest(BaseModel):
    title: str | None = None


class SetModeRequest(BaseModel):
    modeId: str


class SetModelRequest(BaseModel):
    modelId: str


class ExecuteCommandRequest(BaseModel):
    command: str
    args: dict[str, Any] | None = None


class CreateTaskResponse(BaseModel):
    taskId: str
    agentSessionId: str
    runUrl: str
    eventsUrl: str
    modes: list[dict[str, str]] | None = None
    models: list[dict[str, str]] | None = None
    currentModeId: str | None = None


class StartRunResponse(BaseModel):
    runId: str


class TaskSummary(BaseModel):
    taskId: str
    agentSessionId: str
    cwd: str
    title: str
    createdAt: str
    updatedAt: str
    status: Literal["idle", "running", "error"] = "idle"


class TaskListResponse(BaseModel):
    tasks: list[TaskSummary]
