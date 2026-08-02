"""Unit tests for ``MessageSnapshotAccumulator``.

These are fast, ACP-independent tests of the fold logic — they feed
synthetic ``AguiEvent`` sequences and assert the resulting
``SnapshotMessage`` list. They cover the cases the proposal calls out:

  - basic text / reasoning / tool-call / tool-result folding,
  - the "orphaned tool call gets a synthetic result at turn end" case
    that today's replay path drops silently,
  - the ``add_user_text`` direct entry point,
  - and the no-op events (``RUN_*``, ``CUSTOM``, ``STATE_*``,
    ``REASONING_START``/``END`` phase brackets, ``TOOL_CALL_END``).
"""

import json

from agui_on_acp.agui.events import (
    CustomEvent,
    MessagesSnapshotEvent,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    RunFinishedEvent,
    RunStartedEvent,
    SnapshotMessage,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from agui_on_acp.bridge.snapshot_accumulator import MessageSnapshotAccumulator


def _roles(acc: MessageSnapshotAccumulator) -> list[str]:
    """Return the role list of the accumulator's snapshot — convenience for
    ordering assertions."""
    return [m.role for m in acc.snapshot()]


def test_text_message_start_content_end_one_assistant_message():
    """START + N CONTENT + END folds into one assistant message with the
    concatenated content."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageStartEvent(messageId="m1"))
    acc.fold(TextMessageContentEvent(messageId="m1", delta="hello "))
    acc.fold(TextMessageContentEvent(messageId="m1", delta="world"))
    acc.fold(TextMessageEndEvent(messageId="m1"))
    msgs = acc.snapshot()
    assert len(msgs) == 1
    assert msgs[0].role == "assistant"
    assert msgs[0].id == "m1"
    assert msgs[0].content == "hello world"


def test_two_text_messages_are_separate():
    """Two START/END-bracketed text streams fold into two distinct assistant
    messages keyed by their ids."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageStartEvent(messageId="m1"))
    acc.fold(TextMessageContentEvent(messageId="m1", delta="first"))
    acc.fold(TextMessageEndEvent(messageId="m1"))
    acc.fold(TextMessageStartEvent(messageId="m2"))
    acc.fold(TextMessageContentEvent(messageId="m2", delta="second"))
    acc.fold(TextMessageEndEvent(messageId="m2"))
    msgs = acc.snapshot()
    assert [m.content for m in msgs] == ["first", "second"]
    assert [m.id for m in msgs] == ["m1", "m2"]


def test_reasoning_message_folded_as_role_reasoning():
    """``REASONING_MESSAGE_*`` folds into a ``role="reasoning"`` message."""
    acc = MessageSnapshotAccumulator()
    acc.fold(ReasoningStartEvent(messageId="r1"))
    acc.fold(ReasoningMessageStartEvent(messageId="r1"))
    acc.fold(ReasoningMessageContentEvent(messageId="r1", delta="thinking"))
    acc.fold(ReasoningMessageEndEvent(messageId="r1"))
    acc.fold(ReasoningEndEvent(messageId="r1"))
    msgs = acc.snapshot()
    assert len(msgs) == 1
    assert msgs[0].role == "reasoning"
    assert msgs[0].content == "thinking"


def test_reasoning_phase_brackets_are_no_op():
    """``REASONING_START``/``REASONING_END`` phase brackets don't mint
    messages — only ``REASONING_MESSAGE_*`` does."""
    acc = MessageSnapshotAccumulator()
    acc.fold(ReasoningStartEvent(messageId="r1"))
    acc.fold(ReasoningEndEvent(messageId="r1"))
    assert not acc.snapshot()


def test_tool_call_attaches_to_trailing_assistant_message():
    """``TOOL_CALL_START``/``ARGS``/``END``/``RESULT`` folds into an
    assistant message carrying the tool call plus a trailing
    ``role="tool"`` result message."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageStartEvent(messageId="a1"))
    acc.fold(TextMessageContentEvent(messageId="a1", delta="running bash"))
    acc.fold(TextMessageEndEvent(messageId="a1"))
    acc.fold(
        ToolCallStartEvent(
            toolCallId="tc1", toolCallName="bash: ls", parentMessageId="a1"
        )
    )
    acc.fold(ToolCallArgsEvent(toolCallId="tc1", delta='{"command": "ls"}'))
    acc.fold(ToolCallEndEvent(toolCallId="tc1"))
    acc.fold(
        ToolCallResultEvent(messageId="tc1-result", toolCallId="tc1", content="ok")
    )
    msgs = acc.snapshot()
    assert _roles(acc) == ["assistant", "tool"]
    assistant = msgs[0]
    assert assistant.id == "a1"
    assert assistant.content == "running bash"
    assert assistant.toolCalls is not None
    assert len(assistant.toolCalls) == 1
    call = assistant.toolCalls[0]
    assert call.id == "tc1"
    assert call.function["name"] == "bash: ls"
    assert json.loads(call.function["arguments"]) == {"command": "ls"}
    tool_msg = msgs[1]
    assert tool_msg.role == "tool"
    assert tool_msg.toolCallId == "tc1"
    assert tool_msg.content == "ok"


def test_tool_call_with_no_trailing_assistant_creates_one():
    """A tool call arriving before any text opens a fresh empty assistant
    message and attaches to it (mirrors the legacy replay coalescer)."""
    acc = MessageSnapshotAccumulator()
    acc.fold(
        ToolCallStartEvent(toolCallId="tc1", toolCallName="bash", parentMessageId=None)
    )
    acc.fold(ToolCallArgsEvent(toolCallId="tc1", delta='{"command": "ls"}'))
    acc.fold(ToolCallEndEvent(toolCallId="tc1"))
    acc.fold(
        ToolCallResultEvent(messageId="tc1-result", toolCallId="tc1", content="ok")
    )
    msgs = acc.snapshot()
    assert _roles(acc) == ["assistant", "tool"]
    assert msgs[0].content in (None, "")
    assert msgs[0].toolCalls is not None
    assert len(msgs[0].toolCalls) == 1


def test_tool_call_args_default_to_empty_object_when_no_args_event():
    """If ``TOOL_CALL_START`` flushes but no ``TOOL_CALL_ARGS`` arrives
    (e.g. orphaned tool call closed at turn end), the arguments default to
    ``"{}"`` — matching what the live ``_close_all_tool_calls`` path
    synthesises."""
    acc = MessageSnapshotAccumulator()
    acc.fold(
        ToolCallStartEvent(toolCallId="tc1", toolCallName="bash", parentMessageId=None)
    )
    acc.fold(ToolCallEndEvent(toolCallId="tc1"))
    acc.fold(ToolCallResultEvent(messageId="tc1-result", toolCallId="tc1", content=""))
    msgs = acc.snapshot()
    assert msgs[0].toolCalls is not None
    assert json.loads(msgs[0].toolCalls[0].function["arguments"]) == {}


def test_orphaned_tool_call_gets_synthetic_result_at_turn_end():
    """The proposal's headline correctness fix: a tool call that started
    but never received a completion now gets a synthetic (empty) result at
    turn end via the live ``_close_all_tool_calls`` path folding through
    the accumulator. Today's replay path silently drops these."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageStartEvent(messageId="a1"))
    acc.fold(TextMessageContentEvent(messageId="a1", delta="running tool"))
    acc.fold(TextMessageEndEvent(messageId="a1"))
    acc.fold(
        ToolCallStartEvent(toolCallId="tc1", toolCallName="bash", parentMessageId="a1")
    )
    acc.fold(ToolCallArgsEvent(toolCallId="tc1", delta='{"command": "ls"}'))
    # No TOOL_CALL_END / TOOL_CALL_RESULT arrives before turn end — the
    # live bridge's _close_all_tool_calls synthesises them.
    acc.fold(ToolCallEndEvent(toolCallId="tc1"))
    acc.fold(ToolCallResultEvent(messageId="tc1-result", toolCallId="tc1", content=""))
    msgs = acc.snapshot()
    assert _roles(acc) == ["assistant", "tool"]
    tool_msg = msgs[1]
    assert tool_msg.role == "tool"
    assert tool_msg.toolCallId == "tc1"
    assert tool_msg.content == ""


def test_multiple_tool_calls_separated_by_results_get_their_own_assistant_message():
    """Two tool calls separated by a tool result each attach to their own
    assistant message — the accumulator's "trailing assistant message"
    rule (per the proposal) treats the intervening ``role="tool"`` result
    as breaking the association. This matches the existing replay
    coalescer's ``_append_replay_tool_start`` trailing-message check."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageStartEvent(messageId="a1"))
    acc.fold(TextMessageContentEvent(messageId="a1", delta="doing two things"))
    acc.fold(TextMessageEndEvent(messageId="a1"))
    for tid in ("tc1", "tc2"):
        acc.fold(
            ToolCallStartEvent(
                toolCallId=tid, toolCallName="bash", parentMessageId="a1"
            )
        )
        acc.fold(ToolCallArgsEvent(toolCallId=tid, delta='{"command": "x"}'))
        acc.fold(ToolCallEndEvent(toolCallId=tid))
        acc.fold(
            ToolCallResultEvent(messageId=f"{tid}-result", toolCallId=tid, content="ok")
        )
    msgs = acc.snapshot()
    assert _roles(acc) == ["assistant", "tool", "assistant", "tool"]
    # Each assistant message carries exactly one tool call.
    assistant_msgs = [m for m in msgs if m.role == "assistant"]
    assert all(
        m.toolCalls is not None and len(m.toolCalls) == 1 for m in assistant_msgs
    )
    assert [(m.toolCalls[0].id if m.toolCalls else None) for m in assistant_msgs] == [
        "tc1",
        "tc2",
    ]


def test_contiguous_tool_calls_share_one_assistant_message():
    """Two ``TOOL_CALL_START`` events arriving back-to-back (no intervening
    ``TOOL_CALL_RESULT``) attach to the same trailing assistant message —
    the "trailing assistant message" rule only opens a fresh one when the
    trailing message isn't an assistant message."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageStartEvent(messageId="a1"))
    acc.fold(TextMessageContentEvent(messageId="a1", delta="parallel calls"))
    acc.fold(TextMessageEndEvent(messageId="a1"))
    for tid in ("tc1", "tc2"):
        acc.fold(
            ToolCallStartEvent(
                toolCallId=tid, toolCallName="bash", parentMessageId="a1"
            )
        )
        acc.fold(ToolCallArgsEvent(toolCallId=tid, delta='{"command": "x"}'))
    msgs = acc.snapshot()
    assert _roles(acc) == ["assistant"]
    assert msgs[0].toolCalls is not None
    assert len(msgs[0].toolCalls) == 2


def test_add_user_text_merges_contiguous_user_chunks():
    """Contiguous ``add_user_text`` calls merge into one ``role="user"``
    message (mirrors the legacy replay coalescer's merge behaviour)."""
    acc = MessageSnapshotAccumulator()
    acc.add_user_text("hello ")
    acc.add_user_text("world")
    msgs = acc.snapshot()
    assert len(msgs) == 1
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello world"


def test_add_user_text_separates_from_non_user_trailing():
    """A user chunk following a non-user message opens a fresh
    ``role="user"`` message rather than merging."""
    acc = MessageSnapshotAccumulator()
    acc.add_user_text("first user turn")
    acc.fold(TextMessageStartEvent(messageId="a1"))
    acc.fold(TextMessageContentEvent(messageId="a1", delta="assistant reply"))
    acc.fold(TextMessageEndEvent(messageId="a1"))
    acc.add_user_text("second user turn")
    msgs = acc.snapshot()
    assert _roles(acc) == ["user", "assistant", "user"]
    assert [m.content for m in msgs if m.role == "user"] == [
        "first user turn",
        "second user turn",
    ]


def test_run_custom_state_events_are_no_op():
    """``RUN_*``, ``CUSTOM``, and ``STATE_*`` events fold to nothing."""
    acc = MessageSnapshotAccumulator()
    acc.fold(RunStartedEvent(runId="r1", taskId="t1", threadId="t1"))
    acc.fold(CustomEvent(name="agent:usage", value={"used": 1}))
    acc.fold(StateSnapshotEvent(snapshot={"modes": []}))
    acc.fold(RunFinishedEvent(runId="r1", taskId="t1", threadId="t1"))
    assert not acc.snapshot()


def test_messages_snapshot_event_is_no_op():
    """A nested ``MESSAGES_SNAPSHOT`` event (never produced by the bridge
    but defensive) is silently dropped — folding a snapshot into a
    snapshot has no meaning."""
    acc = MessageSnapshotAccumulator()
    acc.fold(MessagesSnapshotEvent(messages=[]))
    assert not acc.snapshot()


def test_interleaved_reasoning_text_tool_in_transcript_order():
    """Mirrors the integration test
    ``test_connect_replay_interleaves_reasoning_in_messages_snapshot`` but
    driven purely with wire events — proving the accumulator is what
    produces the interleaved ordering, independent of ACP."""
    acc = MessageSnapshotAccumulator()
    acc.add_user_text("do thing A and B")
    # thought(A) → reasoning
    acc.fold(ReasoningStartEvent(messageId="r1"))
    acc.fold(ReasoningMessageStartEvent(messageId="r1"))
    acc.fold(ReasoningMessageContentEvent(messageId="r1", delta="thinking about A"))
    acc.fold(ReasoningMessageEndEvent(messageId="r1"))
    acc.fold(ReasoningEndEvent(messageId="r1"))
    # text(A) → assistant carrying tool call
    acc.fold(TextMessageStartEvent(messageId="a1"))
    acc.fold(TextMessageContentEvent(messageId="a1", delta="doing A"))
    acc.fold(TextMessageEndEvent(messageId="a1"))
    acc.fold(
        ToolCallStartEvent(toolCallId="tc1", toolCallName="bash", parentMessageId="a1")
    )
    acc.fold(ToolCallArgsEvent(toolCallId="tc1", delta='{"command": "x"}'))
    acc.fold(ToolCallEndEvent(toolCallId="tc1"))
    acc.fold(
        ToolCallResultEvent(messageId="tc1-result", toolCallId="tc1", content="ok")
    )
    # thought(B) → reasoning
    acc.fold(ReasoningStartEvent(messageId="r2"))
    acc.fold(ReasoningMessageStartEvent(messageId="r2"))
    acc.fold(ReasoningMessageContentEvent(messageId="r2", delta="thinking about B"))
    acc.fold(ReasoningMessageEndEvent(messageId="r2"))
    acc.fold(ReasoningEndEvent(messageId="r2"))
    # text(B) → assistant
    acc.fold(TextMessageStartEvent(messageId="a2"))
    acc.fold(TextMessageContentEvent(messageId="a2", delta="doing B"))
    acc.fold(TextMessageEndEvent(messageId="a2"))
    assert _roles(acc) == [
        "user",
        "reasoning",
        "assistant",
        "tool",
        "reasoning",
        "assistant",
    ]
    reasoning = [m for m in acc.snapshot() if m.role == "reasoning"]
    assert [m.content for m in reasoning] == ["thinking about A", "thinking about B"]


def test_snapshot_returns_a_copy():
    """``snapshot()`` returns a shallow copy so the caller can't mutate
    the accumulator's internal state."""
    acc = MessageSnapshotAccumulator()
    acc.add_user_text("hi")
    snap = acc.snapshot()
    snap.append(SnapshotMessage(id="x", role="user", content="mutated"))
    # The accumulator's internal list is unaffected.
    assert len(acc.snapshot()) == 1


def test_text_message_content_for_unknown_message_id_is_dropped():
    """A ``TEXT_MESSAGE_CONTENT`` whose ``messageId`` has no open
    ``START`` (e.g. an END already closed it, or it never opened) is
    silently dropped — defensive against malformed streams."""
    acc = MessageSnapshotAccumulator()
    acc.fold(TextMessageContentEvent(messageId="never-opened", delta="orphan"))
    assert not acc.snapshot()
