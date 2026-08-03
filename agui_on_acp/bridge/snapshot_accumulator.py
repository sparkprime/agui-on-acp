"""``MessageSnapshotAccumulator`` — folds AG-UI wire events into a
``list[SnapshotMessage]`` (and a parallel ``dict[str, Any]`` state) for
``MESSAGES_SNAPSHOT`` / ``STATE_SNAPSHOT`` replay.

This component is the single fold point that turns the *live* AG-UI event
stream back into whole-message snapshots during ``session/load`` replay.
It knows nothing about ACP, the bridge's replay-mode flag, or the live
sequencing rules — it purely folds the AG-UI wire vocabulary
(``TEXT_MESSAGE_*``, ``REASONING_MESSAGE_*``, ``TOOL_CALL_*``) into a
message list, and ``STATE_SNAPSHOT`` / ``STATE_DELTA`` (JSON Patch,
RFC 6902) into a state dict. The bridge's live state machine remains the
only implementation of the sequencing rules; during replay the bridge
intercepts each event at ``_emit()`` and folds it here instead of
putting it on the SSE queue, then emits one ``MESSAGES_SNAPSHOT`` (plus a
final ``STATE_SNAPSHOT`` from the accumulated state) from
``end_replay()``. A future sequencing fix in the live handlers
automatically applies to replay too, since there is nothing
replay-specific left to keep in sync.

The one entry point that is NOT a wire-event fold is ``add_user_text``:
AG-UI's ``TextMessageStartEvent.role`` is hardcoded ``"assistant"``, so
there is no wire event to fold for a ``UserMessageChunk``. The bridge
calls ``add_user_text`` directly during replay (the single retained
live/replay behavioural difference — see ``acp_to_agui.py``'s
``_handle_user_message_chunk_typed``).

State handling
--------------

``STATE_SNAPSHOT`` replaces the accumulated state wholesale (per the AG-UI
spec, ``state.mdx:54-55`` — "replace the whole state model"). ``STATE_DELTA``
applies a JSON Patch (RFC 6902) delta; the applier is lenient in the same
way the reference client's `fast-json-patch` is: `replace` on a missing
path falls back to `add`, and `remove` on a missing path is a no-op. The
state is seeded with ``{plans: {}, usage: None, sessionInfo: None}`` so
the very first per-field `replace` is valid even under a strict applier
(mirrors the live path's post-`start_run` `STATE_SNAPSHOT` baseline).
``end_replay()`` emits the accumulated state as one `STATE_SNAPSHOT`
alongside the `MESSAGES_SNAPSHOT` — this is what fixes the reconnect bug
where plan/usage/session_info used to vanish (they were `CUSTOM`
fire-and-forget events that the accumulator's catch-all silently dropped).
"""

import copy
import uuid
from typing import Any

from agui_on_acp.agui.events import (
    AguiEvent,
    AssistantToolCall,
    ReasoningEndEvent,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    ReasoningStartEvent,
    SnapshotMessage,
    StateDeltaEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)

# Baseline state seed: ensures the first STATE_DELTA `replace` on these
# paths is valid even under a strict RFC 6902 applier, and mirrors the
# live path's post-start_run STATE_SNAPSHOT baseline.
_STATE_BASELINE: dict[str, object] = {
    "plans": {},
    "usage": None,
    "sessionInfo": None,
}


class MessageSnapshotAccumulator:
    """Folds AG-UI wire events into a ``list[SnapshotMessage]`` plus a
    ``dict[str, Any]`` state.

    Independently unit-testable without the fake-agent/ACP harness: feed it
    a synthetic sequence of ``AguiEvent`` objects and assert the resulting
    ``SnapshotMessage`` list via ``snapshot()`` and the state via
    ``state_snapshot()``.
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
        # Evolving state, folded from STATE_SNAPSHOT/STATE_DELTA. Seeded
        # with the plan/usage/sessionInfo baseline so the first per-field
        # `replace` is valid under a strict applier.
        self._state: dict[str, Any] = copy.deepcopy(_STATE_BASELINE)

    # ── Wire-event fold ───────────────────────────────────────────────────

    def fold(self, event: AguiEvent) -> None:
        """Fold a single AG-UI event into the snapshot (and state).

        Non-message, non-state events (``RUN_*``, ``CUSTOM``,
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
        elif isinstance(event, StateSnapshotEvent):
            # Full replace per spec — but keep the seed baseline's keys
            # when the snapshot omits them, so a partial baseline-less
            # snapshot doesn't leave plans/usage/sessionInfo undefined
            # for subsequent deltas. The only STATE_SNAPSHOT folded during
            # replay is end_replay's own (built from _state itself), so in
            # practice this branch is defensive.
            merged = copy.deepcopy(_STATE_BASELINE)
            merged.update(event.snapshot)
            self._state = merged
        elif isinstance(event, StateDeltaEvent):
            _apply_json_patch(self._state, event.delta)
        else:
            # RUN_*, CUSTOM, MESSAGES_SNAPSHOT — not message- or state-shaped.
            pass

    # ── Direct (non-wire) entry points ────────────────────────────────────

    def merge_state(self, extra: dict[str, Any]) -> None:
        """Merge response-derived meta (``modes`` / ``currentModeId`` /
        ``configOptions``) into the accumulated state.

        Used by ``connect_session`` to fold the ``LoadSessionResponse``'s
        advertised meta into the replay snapshot — those fields come from
        the RPC response, not from the replayed ``session/update`` stream
        (modes aren't carried by any update kind; configOptions arrive via
        ``ConfigOptionUpdate`` but an agent may not replay one). Only
        non-``None`` entries are merged so absent fields don't clobber
        anything the replay stream already established. The response
        reflects the current (final) state, so merging it after the replay
        stream is delivered is correct and idempotent.
        """
        for key, value in extra.items():
            if value is not None:
                self._state[key] = value

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

    def state_snapshot(self) -> dict[str, Any]:
        """Return a shallow copy of the accumulated state dict."""
        return dict(self._state)

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


# ── JSON Patch (RFC 6902) application — lenient, like fast-json-patch ────


# Re-exported type alias for the operation dict.
JsonPatchOperation = dict[str, Any]


def _apply_json_patch(state: dict[str, Any], delta: list[JsonPatchOperation]) -> None:
    """Apply a JSON Patch delta to ``state`` in place.

    Implements the subset of RFC 6902 the bridge emits: `add`, `replace`,
    and `remove` against object-member paths (no array-index ops — none of
    the bridge's paths touch arrays). Lenient in the same way the reference
    client's `fast-json-patch` is: `replace` on a missing path falls back
    to `add`, and `remove` on a missing path is a no-op — so a client (or
    this accumulator) that hasn't seen a baseline snapshot still converges.
    """
    for op in delta:
        _apply_op(state, op)


def _apply_op(state: dict[str, Any], op: JsonPatchOperation) -> None:
    op_name = op.get("op")
    path = op.get("path")
    if not isinstance(path, str) or path == "":
        return
    tokens = _parse_pointer(path)
    if op_name in ("add", "replace"):
        _set_by_pointer(state, tokens, op.get("value"))
    elif op_name == "remove":
        _remove_by_pointer(state, tokens)
    # Unknown ops are ignored — defensive against malformed deltas.


def _parse_pointer(pointer: str) -> list[str]:
    """Parse a JSON Pointer (RFC 6901) into a list of tokens.

    ``"/plans/default"`` → ``["plans", "default"]``; ``"~0`` → ``~``,
    ``~1`` → ``/``.
    """
    if pointer == "":
        return []
    if pointer[0] != "/":
        return [pointer]
    parts = pointer[1:].split("/")
    return [_unescape_token(p) for p in parts]


def _unescape_token(token: str) -> str:
    # RFC 6901: ~1 → /, ~0 → ~ (order matters — unescape ~1 first).
    return token.replace("~1", "/").replace("~0", "~")


def _escape_pointer_token(token: str) -> str:
    # Inverse of _unescape_token, for building paths from plan ids.
    return token.replace("~", "~0").replace("/", "~1")


def _set_by_pointer(state: dict[str, Any], tokens: list[str], value: Any) -> None:
    """Set the value at the pointer, creating intermediate objects as
    needed. ``replace`` on a missing path is treated as ``add`` (lenient)."""
    if not tokens:
        return
    node: Any = state
    for token in tokens[:-1]:
        nxt = node.get(token) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            nxt = {}
            if isinstance(node, dict):
                node[token] = nxt
            else:
                return
        node = nxt
    if isinstance(node, dict):
        node[tokens[-1]] = value


def _remove_by_pointer(state: dict[str, Any], tokens: list[str]) -> None:
    """Remove the value at the pointer; no-op if the path is missing."""
    if not tokens:
        return
    node: Any = state
    for token in tokens[:-1]:
        nxt = node.get(token) if isinstance(node, dict) else None
        if not isinstance(nxt, dict):
            return
        node = nxt
    if isinstance(node, dict):
        node.pop(tokens[-1], None)
