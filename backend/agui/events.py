"""AG-UI event type definitions.

These are the canonical AG-UI event types emitted over SSE. Each event is a
JSON object with a `type` field identifying the event kind.
"""

from enum import Enum
from typing import Any, Literal

import time
import uuid

from pydantic import BaseModel, Field


class AguiEventType(str, Enum):
    """AG-UI event type enumeration."""

    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    RUN_ERROR = "RUN_ERROR"
    TEXT_MESSAGE_START = "TEXT_MESSAGE_START"
    TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
    TEXT_MESSAGE_END = "TEXT_MESSAGE_END"
    TOOL_CALL_START = "TOOL_CALL_START"
    TOOL_CALL_ARGS = "TOOL_CALL_ARGS"
    TOOL_CALL_END = "TOOL_CALL_END"
    STATE_UPDATE = "STATE_UPDATE"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    CUSTOM = "CUSTOM"


class BaseAguiEvent(BaseModel):
    """Base AG-UI event with common fields."""

    type: AguiEventType
    timestamp: float = Field(default_factory=time.time)
    rawEvent: dict[str, Any] | None = None  # optional original ACP data


class RunStartedEvent(BaseAguiEvent):
    type: Literal[AguiEventType.RUN_STARTED] = AguiEventType.RUN_STARTED
    runId: str
    taskId: str
    threadId: str | None = None


class RunFinishedEvent(BaseAguiEvent):
    type: Literal[AguiEventType.RUN_FINISHED] = AguiEventType.RUN_FINISHED
    runId: str
    taskId: str


class RunErrorEvent(BaseAguiEvent):
    type: Literal[AguiEventType.RUN_ERROR] = AguiEventType.RUN_ERROR
    runId: str
    taskId: str
    message: str
    code: str | None = None


class TextMessageStartEvent(BaseAguiEvent):
    type: Literal[AguiEventType.TEXT_MESSAGE_START] = AguiEventType.TEXT_MESSAGE_START
    messageId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["assistant"] = "assistant"


class TextMessageContentEvent(BaseAguiEvent):
    type: Literal[AguiEventType.TEXT_MESSAGE_CONTENT] = AguiEventType.TEXT_MESSAGE_CONTENT
    messageId: str
    delta: str


class TextMessageEndEvent(BaseAguiEvent):
    type: Literal[AguiEventType.TEXT_MESSAGE_END] = AguiEventType.TEXT_MESSAGE_END
    messageId: str


class ToolCallStartEvent(BaseAguiEvent):
    type: Literal[AguiEventType.TOOL_CALL_START] = AguiEventType.TOOL_CALL_START
    toolCallId: str
    toolCallName: str
    parentMessageId: str | None = None


class ToolCallArgsEvent(BaseAguiEvent):
    type: Literal[AguiEventType.TOOL_CALL_ARGS] = AguiEventType.TOOL_CALL_ARGS
    toolCallId: str
    delta: str  # JSON string chunk of args


class ToolCallEndEvent(BaseAguiEvent):
    type: Literal[AguiEventType.TOOL_CALL_END] = AguiEventType.TOOL_CALL_END
    toolCallId: str
    result: str | None = None


class StateUpdateEvent(BaseAguiEvent):
    type: Literal[AguiEventType.STATE_UPDATE] = AguiEventType.STATE_UPDATE
    state: dict[str, Any]  # arbitrary JSON state


class StateSnapshotEvent(BaseAguiEvent):
    type: Literal[AguiEventType.STATE_SNAPSHOT] = AguiEventType.STATE_SNAPSHOT
    snapshot: dict[str, Any]


class CustomEvent(BaseAguiEvent):
    type: Literal[AguiEventType.CUSTOM] = AguiEventType.CUSTOM
    name: str
    value: dict[str, Any] = Field(default_factory=dict)


# Union type for all events
AguiEvent = (
    RunStartedEvent
    | RunFinishedEvent
    | RunErrorEvent
    | TextMessageStartEvent
    | TextMessageContentEvent
    | TextMessageEndEvent
    | ToolCallStartEvent
    | ToolCallArgsEvent
    | ToolCallEndEvent
    | StateUpdateEvent
    | StateSnapshotEvent
    | CustomEvent
)
