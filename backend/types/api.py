"""API type definitions for the ACP → AG-UI Bridge backend."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response body for health check."""

    status: Literal["ok"] = "ok"
    version: str
    project: str = "acp-to-agui"
