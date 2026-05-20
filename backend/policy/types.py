"""Policy decision types for tool-call approval."""

from typing import Literal

from pydantic import BaseModel


class PolicyDecision(BaseModel):
    """Result of a policy evaluation for a tool call."""

    requires_approval: bool
    reason: str | None = None
    category: Literal["filesystem", "command", "network", "mcp", "other"] = "other"
