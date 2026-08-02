"""``MessageSnapshotAccumulator`` — folds AG-UI wire events into a
``list[SnapshotMessage]`` for ``MESSAGES_SNAPSHOT`` replay.

This component is the single fold point that turns the *live* AG-UI event
stream back into a whole-message snapshot during ``session/load`` replay.
It knows nothing about ACP, the bridge's replay-mode flag, or the live
sequencing rules — it purely folds the AG-UI wire vocabulary
(``TEXT_MESSAGE_*``, ``REASONING_MESSAGE_*``, ``TOOL_CALL_*``) into a
message list. The bridge's live state machine remains the only
implementation of the sequencing rules; during replay the bridge
intercepts each event at ``_emit()`` and folds it here instead of
putting it on the SSE queue, then emits one ``MESSAGES_SNAPSHOT`` from
``end_replay()``. A future sequencing fix in the live handlers
automatically applies to replay too, since there is nothing
replay-specific left to keep in sync.

The one entry point that is NOT a wire-event fold is ``add_user_text``:
AG-UI's ``TextMessageStartEvent.role`` is hardcoded ``"assistant"``, so
there is no wire event to fold for a ``UserMessageChunk``. The bridge
calls ``add_user_text`` directly during replay (the single retained
live/replay behavioural difference — see ``acp_to_agui.py``'s
``_handle_user_message_chunk_typed``).
"""

import uuid

from agui_on_acp.agui.events import (
    AguiEvent,
    AssistantToolCall,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    SnapshotMessage,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)


class MessageSnapshotAccumulator:
    """Folds AG-UI wire events into a ``list[SnapshotMessage]``.

    Independently unit-testable without the fake-agent/ACP harness: feed it
    a synthetic sequence of ``AguiEvent`` objects and assert the resulting
    ``SnapshotMessage`` list via ``snapshot()``.
    """

    def __init__(self) -> None:
        self._messages: list[SnapshotMessage] = []
        # Map messageId → index in _messages for the currently-open text or
        # reasoning message. Popped on the matching END so a subsequent
        # START opens a fresh message even if the ids reused.
        self._open_message_idx: dict[str, int] = {}
        # Map toolCallId → (msg_idx, tc_idx) so TOOL_CALL_ARGS can locate
        # the tool call to set its arguments.
        self._tool_call_locations: dict[str, tuple[int, int]] = {}

    # ── Wire-event fold ───────────────────────────────────────────────────

    def fold(self, event: AguiEvent) -> None:
        """Fold a single AG-UI event into the snapshot.

        Non-message-shaped events (``RUN_*``, ``CUSTOM``, ``STATE_*``,
        ``MESSAGES_SNAPSHOT``, ``REASONING_START``/``END`` phase brackets,
        ``TOOL_CALL_END``) are silently no-op'd — folding them has no
        meaning.
        """
        if isinstance(event, TextMessageStartEvent):
            self._open_message_idx[event.messageId] = len(self._messages)
            self._messages.append(
                SnapshotMessage(id=event.messageId, role="assistant", content="")
            )
        elif isinstance(event, TextMessageContentEvent):
            idx = self._open_message_idx.get(event.messageId)
            if idx is not None:
                msg = self._messages[idx]
                msg.content = (msg.content or "") + event.delta
        elif isinstance(event, TextMessageEndEvent):
            self._open_message_idx.pop(event.messageId, None)
        elif isinstance(event, ReasoningMessageStartEvent):
            self._open_message_idx[event.messageId] = len(self._messages)
            self._messages.append(
                SnapshotMessage(id=event.messageId, role="reasoning", content="")
            )
        elif isinstance(event, ReasoningMessageContentEvent):
            idx = self._open_message_idx.get(event.messageId)
            if idx is not None:
                msg = self._messages[idx]
                msg.content = (msg.content or "") + event.delta
        elif isinstance(event, ReasoningMessageEndEvent):
            self._open_message_idx.pop(event.messageId, None)
        elif isinstance(event, (ReasoningStartEvent, ReasoningEndEvent)):
            # Phase brackets — not message-shaped.
            pass
        elif isinstance(event, ToolCallStartEvent):
            self._attach_tool_call_start(
                tool_call_id=event.toolCallId,
                tool_call_name=event.toolCallName,
                parent_message_id=event.parentMessageId,
            )
        elif isinstance(event, ToolCallArgsEvent):
            self._set_tool_call_args(event.toolCallId, event.delta)
        elif isinstance(event, ToolCallResultEvent):
            self._messages.append(
                SnapshotMessage(
                    id=event.messageId,
                    role="tool",
                    content=event.content,
                    toolCallId=event.toolCallId,
                )
            )
        elif isinstance(event, ToolCallEndEvent):
            # End-of-args-streaming signal — not message-shaped.
            pass
        else:
            # RUN_*, CUSTOM, STATE_*, MESSAGES_SNAPSHOT — not message-shaped.
            pass

    # ── Direct (non-wire) entry point ─────────────────────────────────────

    def add_user_text(self, text: str) -> None:
        """Append a user text delta, merging with the trailing message when
        it is already a ``role="user"`` message (mirrors the legacy replay
        coalescer's merge behaviour for contiguous user chunks).

        Called directly by the bridge's ``_handle_user_message_chunk_*``
        during replay — AG-UI's ``TextMessageStartEvent.role`` is hardcoded
        ``"assistant"``, so there is no wire event to fold for a user chunk.
        """
        last = self._messages[-1] if self._messages else None
        if last is not None and last.role == "user":
            last.content = (last.content or "") + text
            return
        self._messages.append(
            SnapshotMessage(id=str(uuid.uuid4()), role="user", content=text)
        )

    # ── Read-out ──────────────────────────────────────────────────────────

    def snapshot(self) -> list[SnapshotMessage]:
        """Return a shallow copy of the accumulated message list."""
        return list(self._messages)

    # ── Internal helpers ──────────────────────────────────────────────────

    def _attach_tool_call_start(
        self,
        tool_call_id: str,
        tool_call_name: str,
        parent_message_id: str | None,
    ) -> None:
        """Attach an ``AssistantToolCall`` to the trailing assistant message
        (or start a fresh empty one if the trailing message isn't an
        assistant message), and track the tool call's position so
        ``TOOL_CALL_ARGS`` can find it.

        ``parent_message_id`` is the AG-UI ``TOOL_CALL_START.parentMessageId``
        — used as the id of the fresh assistant message when one is created,
        so it matches the live stream's framing. When the trailing message
        is already an assistant message, the tool call attaches to it
        regardless of ``parent_message_id`` (matches the legacy replay
        coalescer's "trailing assistant" rule).
        """
        last = self._messages[-1] if self._messages else None
        if last is None or last.role != "assistant":
            last = SnapshotMessage(
                id=parent_message_id or str(uuid.uuid4()),
                role="assistant",
            )
            self._messages.append(last)
        tool_calls = last.toolCalls
        if tool_calls is None:
            tool_calls = []
            setattr(last, "toolCalls", tool_calls)
        call = AssistantToolCall(
            id=tool_call_id,
            function={"name": tool_call_name, "arguments": "{}"},
        )
        tool_calls.append(call)
        msg_idx = len(self._messages) - 1
        tc_idx = len(tool_calls) - 1
        self._tool_call_locations[tool_call_id] = (msg_idx, tc_idx)

    def _set_tool_call_args(self, tool_call_id: str, args_json: str) -> None:
        """Set the tool call's ``function["arguments"]`` (single-shot, not
        append — the bridge already guarantees exactly one ``TOOL_CALL_ARGS``
        delta per call via ``_tool_args_emitted``)."""
        loc = self._tool_call_locations.get(tool_call_id)
        if loc is None:
            return
        msg_idx, tc_idx = loc
        if msg_idx >= len(self._messages):
            return
        msg = self._messages[msg_idx]
        if not msg.toolCalls or tc_idx >= len(msg.toolCalls):
            return
        msg.toolCalls[tc_idx].function["arguments"] = args_json
