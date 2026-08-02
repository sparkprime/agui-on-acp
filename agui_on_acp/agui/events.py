"""AG-UI event type definitions.

These are the canonical AG-UI event types emitted over SSE. Each event is a
JSON object with a `type` field identifying the event kind.
"""

import time
import uuid
from enum import Enum
from typing import Any, Literal

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
    TOOL_CALL_RESULT = "TOOL_CALL_RESULT"
    STATE_UPDATE = "STATE_UPDATE"
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    CUSTOM = "CUSTOM"
    MESSAGES_SNAPSHOT = "MESSAGES_SNAPSHOT"
    REASONING_START = "REASONING_START"
    REASONING_MESSAGE_START = "REASONING_MESSAGE_START"
    REASONING_MESSAGE_CONTENT = "REASONING_MESSAGE_CONTENT"
    REASONING_MESSAGE_END = "REASONING_MESSAGE_END"
    REASONING_END = "REASONING_END"


class BaseAguiEvent(BaseModel):
    """Base AG-UI event with common fields.

    Each concrete subclass declares its own ``type`` field as a narrowed
    ``Literal[AguiEventType.X]`` rather than overriding a base ``type``.
    Narrowing a mutable pydantic field from ``AguiEventType`` to a single
    literal would violate pyright's invariant-override check, so the base
    intentionally omits ``type`` and leaves it to the subclasses.
    """

    timestamp: float = Field(default_factory=time.time)
    rawEvent: dict[str, Any] | None = None  # optional original ACP data


class RunStartedEvent(BaseAguiEvent):
    """Signals the start of an AG-UI run."""

    type: Literal[AguiEventType.RUN_STARTED] = AguiEventType.RUN_STARTED
    runId: str
    taskId: str
    threadId: str | None = None


class Interrupt(BaseModel):
    """AG-UI Interrupt — surfaces a permission/approval point to the client.

    Schema matches ag-ui types.ts:193. `id` is the correlation key threaded
    through the whole flow: interrupt.id === toolCallId === ACP permission
    callId. `reason` is required by the AG-UI schema.
    """

    id: str
    reason: str
    message: str | None = None
    toolCallId: str | None = None
    responseSchema: dict[str, Any] | None = None
    expiresAt: str | None = None
    metadata: dict[str, Any] | None = None


class InterruptOutcome(BaseModel):
    """AG-UI RunFinishedInterruptOutcome (events.ts:226).

    `.strict()` on the TS side — no extra keys — and `interrupts` must have
    ≥ 1 element (min_length=1 here enforces that before serialization).
    """

    type: Literal["interrupt"] = "interrupt"
    interrupts: list[Interrupt] = Field(min_length=1)


class RunFinishedEvent(BaseAguiEvent):
    """Signals the end of an AG-UI run (optionally with an interrupt outcome)."""

    type: Literal[AguiEventType.RUN_FINISHED] = AguiEventType.RUN_FINISHED
    runId: str
    taskId: str
    threadId: str | None = None
    outcome: InterruptOutcome | None = None


class RunErrorEvent(BaseAguiEvent):
    """Signals a run-ending error."""

    type: Literal[AguiEventType.RUN_ERROR] = AguiEventType.RUN_ERROR
    runId: str
    taskId: str
    message: str
    code: str | None = None
    threadId: str | None = None


class TextMessageStartEvent(BaseAguiEvent):
    """Marks the beginning of an assistant text message."""

    type: Literal[AguiEventType.TEXT_MESSAGE_START] = AguiEventType.TEXT_MESSAGE_START
    messageId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["assistant"] = "assistant"


class TextMessageContentEvent(BaseAguiEvent):
    """A text delta for the currently-open assistant message."""

    type: Literal[AguiEventType.TEXT_MESSAGE_CONTENT] = (
        AguiEventType.TEXT_MESSAGE_CONTENT
    )
    messageId: str
    delta: str


class TextMessageEndEvent(BaseAguiEvent):
    """Marks the end of an assistant text message."""

    type: Literal[AguiEventType.TEXT_MESSAGE_END] = AguiEventType.TEXT_MESSAGE_END
    messageId: str


class ReasoningStartEvent(BaseAguiEvent):
    """Marks the beginning of a reasoning phase (AG-UI ``REASONING_START``).

    Brackets a whole reasoning phase; a phase contains one
    ``ReasoningMessageStart/Content*/End`` sequence. Emitted on the first
    ``AgentThoughtChunk`` of a contiguous run and closed by
    ``ReasoningEndEvent`` on the same lifecycle triggers that close an open
    text message (tool call start, turn end, run finish/error).
    """

    type: Literal[AguiEventType.REASONING_START] = AguiEventType.REASONING_START
    messageId: str


class ReasoningMessageStartEvent(BaseAguiEvent):
    """Marks the beginning of a single reasoning message within a phase."""

    type: Literal[AguiEventType.REASONING_MESSAGE_START] = (
        AguiEventType.REASONING_MESSAGE_START
    )
    messageId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["reasoning"] = "reasoning"


class ReasoningMessageContentEvent(BaseAguiEvent):
    """A text delta for the currently-open reasoning message."""

    type: Literal[AguiEventType.REASONING_MESSAGE_CONTENT] = (
        AguiEventType.REASONING_MESSAGE_CONTENT
    )
    messageId: str
    delta: str


class ReasoningMessageEndEvent(BaseAguiEvent):
    """Marks the end of a single reasoning message within a phase."""

    type: Literal[AguiEventType.REASONING_MESSAGE_END] = (
        AguiEventType.REASONING_MESSAGE_END
    )
    messageId: str


class ReasoningEndEvent(BaseAguiEvent):
    """Marks the end of a reasoning phase."""

    type: Literal[AguiEventType.REASONING_END] = AguiEventType.REASONING_END
    messageId: str


class ToolCallStartEvent(BaseAguiEvent):
    """Marks the beginning of a tool call."""

    type: Literal[AguiEventType.TOOL_CALL_START] = AguiEventType.TOOL_CALL_START
    toolCallId: str
    toolCallName: str
    parentMessageId: str | None = None


class ToolCallArgsEvent(BaseAguiEvent):
    """A JSON-string delta of arguments for a tool call."""

    type: Literal[AguiEventType.TOOL_CALL_ARGS] = AguiEventType.TOOL_CALL_ARGS
    toolCallId: str
    delta: str  # JSON string chunk of args


class ToolCallEndEvent(BaseAguiEvent):
    """Marks the end of a tool call (end-of-args-streaming)."""

    type: Literal[AguiEventType.TOOL_CALL_END] = AguiEventType.TOOL_CALL_END
    toolCallId: str
    result: str | None = None


class ToolCallResultEvent(BaseAguiEvent):
    """AG-UI event carrying the actual output of a completed tool call.

    Distinct from ``ToolCallEndEvent`` (which only signals end-of-args-streaming):
    CopilotKit's runtime listens for ``TOOL_CALL_RESULT`` to synthesize a
    ``ToolMessage`` (role="tool") in its message store, which is what flips a
    tool-call renderer from ``inProgress`` to ``complete`` and surfaces the
    ``result`` string. Without this event the renderer stays stuck at
    ``inProgress`` with empty ``parameters`` forever, even though the agent has
    long since finished executing the tool.

    Schema: ``{ messageId, type, toolCallId, content, role?="tool" }``
    """

    type: Literal[AguiEventType.TOOL_CALL_RESULT] = AguiEventType.TOOL_CALL_RESULT
    messageId: str
    toolCallId: str
    content: str
    role: Literal["tool"] = "tool"


class StateUpdateEvent(BaseAguiEvent):
    """An incremental state update (partial merge)."""

    type: Literal[AguiEventType.STATE_UPDATE] = AguiEventType.STATE_UPDATE
    state: dict[str, Any]  # arbitrary JSON state


class StateSnapshotEvent(BaseAguiEvent):
    """A full state snapshot (replaces all prior state)."""

    type: Literal[AguiEventType.STATE_SNAPSHOT] = AguiEventType.STATE_SNAPSHOT
    snapshot: dict[str, Any]


class CustomEvent(BaseAguiEvent):
    """A vendor-extension event not covered by a standard AG-UI type."""

    type: Literal[AguiEventType.CUSTOM] = AguiEventType.CUSTOM
    name: str
    value: dict[str, Any] = {}


class AssistantToolCall(BaseModel):
    """A tool call embedded in an assistant message snapshot.

    Mirrors ``ag-ui/sdks/typescript/packages/core/src/types.ts``'s
    ``ToolCall`` shape (the ``function`` sub-object carries ``name`` and
    ``arguments`` — the latter a JSON-serialised string).
    """

    id: str
    type: Literal["function"] = "function"
    function: dict[str, Any]


class SnapshotMessage(BaseModel):
    """One message in a ``MESSAGES_SNAPSHOT`` event.

    ``role`` matches the AG-UI ``Message`` discriminated union
    (``ag-ui/.../core/src/types.ts:161-169``), which already defines
    ``reasoning`` as one of its variants (``ReasoningMessageSchema``) —
    this is not a bridge-local addition. Using it lets a replayed
    transcript carry ``AgentThoughtChunk`` content in its correct
    interleaved position rather than as a separate pre-snapshot stream
    (which the ag-ui client would render concatenated at the top — see
    the ``MESSAGES_SNAPSHOT`` handler in ``ag-ui/.../client/src/apply/
    default.ts``: when the snapshot carries no reasoning, streamed
    reasoning is preserved in place). Only ``role="tool"`` messages carry
    ``toolCallId``; assistant messages may carry ``toolCalls``.
    """

    id: str
    role: Literal["user", "assistant", "tool", "system", "developer", "reasoning"]
    content: str | None = None
    # camelCase field names match the AG-UI wire schema (ag-ui types.ts) —
    # they must stay camelCase for client compatibility.
    toolCalls: list[AssistantToolCall] | None = None  # pylint: disable=invalid-name
    toolCallId: str | None = None  # pylint: disable=invalid-name


class MessagesSnapshotEvent(BaseAguiEvent):
    """AG-UI ``MESSAGES_SNAPSHOT`` — replaces the client's entire message
    list. Used to hydrate a transcript on ``connect`` (replay of an existing
    session's history) without re-streaming deltas.
    """

    type: Literal[AguiEventType.MESSAGES_SNAPSHOT] = AguiEventType.MESSAGES_SNAPSHOT
    messages: list[SnapshotMessage]


# Union type for all streamable AG-UI events. ``InterruptOutcome`` is
# intentionally excluded: it is not emitted on the wire itself but nested
# inside ``RunFinishedEvent.outcome``, and its ``type`` is a plain string
# (not an ``AguiEventType`` member) so it does not expose ``.value``.
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
    | ToolCallResultEvent
    | StateUpdateEvent
    | StateSnapshotEvent
    | CustomEvent
    | MessagesSnapshotEvent
    | ReasoningStartEvent
    | ReasoningMessageStartEvent
    | ReasoningMessageContentEvent
    | ReasoningMessageEndEvent
    | ReasoningEndEvent
)
