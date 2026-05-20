"""Session types for multi-session support."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SessionStatus = Literal["idle", "working", "awaiting_approval"]
TaskStatus = Literal["todo", "in-progress", "paused", "completed"]


@dataclass
class ChatMessage:
    role: Literal["user", "agent", "tool"]
    content: str
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())
    tool_name: str | None = None
    tool_purpose: str | None = None
    tool_parameters: dict[str, Any] | None = None
    tool_status: str | None = None
    is_error: bool = False
