"""API type definitions for the AG-UI on ACP bridge."""

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response body for health check."""

    status: Literal["ok"] = "ok"
    version: str
    project: str = "acp-to-agui"
