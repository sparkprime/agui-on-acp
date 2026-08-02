# AG-UI ↔ ACP Mapping

A complete, bidirectional mapping between the AG-UI protocol (what the client
speaks) and ACP (what the agent subprocess speaks), with explicit call-outs
for places where the translation is **not 1:1** and where the bridge must
**hold state** to bridge the gap.

> The bridge's per-run state machine and sequencing rules (one open text
> message at a time, multiple open tool calls, `turn_end` closes everything)
> are documented in the module docstring of
> `agui_on_acp/bridge/acp_to_agui.py`. This doc focuses on the structural
> field mapping and the impedance mismatches.

## Conventions

- **AG-UI → ACP** rows describe what the client sends (HTTP body fields +
  SSE-stream lifecycle) and what ACP call the bridge issues.
- **ACP → AG-UI** rows describe what the agent emits and what AG-UI event
  the bridge synthesises.
- **State held** marks where the bridge keeps something in memory (or on
  disk) to make a non-1:1 translation work; these are the load-bearing
  parts of the proxy.
- **ACP 0.11** tags rows whose mapping changed or whose feature was added
  in `agent-client-protocol` 0.11.

---

## The three conversation operations

The bridge splits the AG-UI conversation lifecycle into three explicit
operations, each backed by a distinct ACP call. This is the central design
decision; every mapping below respects it.

| Operation | Endpoint | ACP call | Notes |
|---|---|---|---|
| **Create** | `POST /ag-ui/sessions` | `session/new` | Spawns a subprocess, mints a fresh `session_id`. The only endpoint that starts a conversation. `cwd` is required (the one genuinely client-decided value). |
| **Connect** | `GET /ag-ui/sessions/{id}/connect` | `session/load` | Replays the existing transcript as a `MESSAGES_SNAPSHOT`. Spawns a short-lived subprocess for the replay; the bridge's `cwd` record (see Persistent State) supplies `cwd` — the client doesn't resend it. |
| **Prompt** | `POST /ag-ui` | `session/prompt` (after `session/resume` or reuse of a live session) | **Attach-only.** Never calls `session/new` or `session/load`. If a live `ActiveSession` exists for the `threadId`, reuses it (no ACP call). Otherwise calls `session/resume` — and if resume is unsupported or the id is dead, yields a hard error (409/404), never a silent fallback to create. |

Plus the management side: `GET /ag-ui/sessions` (`session/list`),
`DELETE /ag-ui/sessions/{id}` (`session/delete`), and
`GET /ag-ui/capabilities`.

### Error model

Pre-stream failures — anything rejected before an SSE stream opens
(unknown session id, unsupported capability, cwd not allowed, no user
message, resume with no pending interrupt) — return plain JSON
`{"error": "..."}` with an appropriate HTTP status code
(400/403/404/409/501). No `text/event-stream` content type, no
`RUN_ERROR` event; a non-SSE content type immediately signals "this isn't
a stream."

Mid-stream failures — errors that occur after a 200 +
`text/event-stream` has started (e.g. the agent's `prompt()` raises
mid-turn) — are surfaced as `RUN_ERROR` SSE events on the already-opened
stream, where the client is already committed to parsing SSE.

---

## AG-UI → ACP (client to agent)

### Run lifecycle

| AG-UI input | ACP call | Notes |
|---|---|---|
| `POST /ag-ui` (fresh, with `messages`) | `session/resume` (or no call if a live `ActiveSession` already exists for the thread) then `session/prompt` | Attach-only — never creates or loads. The caller MUST have created a session first via `POST /ag-ui/sessions`. **State held:** the bridge keeps an `ActiveSession` keyed by `session_id` so the second run on the same thread reuses the spawned subprocess instead of respawning. |
| `POST /ag-ui` with `resume[]` (non-empty) | no new ACP call — resolves a parked `request_permission` / `create_elicitation` Future | The AG-UI client never calls `session/prompt` again on resume; it signals "user decided". The bridge routes the resume entry to `bridge.resolve_interrupt(interruptId, …)`, which unblocks the *original* `session/prompt` task that was parked mid-turn. **1 ACP turn ↔ N+1 AG-UI runs** when N permission points are hit. |
| Client TCP disconnect mid-SSE | `session/cancel` | AG-UI has no explicit cancel verb. The bridge detects disconnect as `CancelledError` in the SSE drain and calls `manager.cancel_run` → `session/cancel` + resolves any parked permission Futures as `cancelled`. |

### `RunAgentInput` fields → ACP

| AG-UI field | ACP effect | Notes |
|---|---|---|
| `threadId` | ACP `session_id` (directly) | `threadId === session_id` — one id, not two. The bridge uses it as the key into `ActiveSession` and the durable `cwd` store. |
| `runId` | ignored | AG-UI lets the client propose a run id; the bridge ignores it and generates its own UUID per run (so it can rotate run ids across suspend/resume). |
| `messages[-1].content` | `session/prompt` `prompt[0] = {type:"text", text:…}` | Only the **last** user message is forwarded; AG-UI's full message history is **not** replayed to ACP (the agent keeps its own session history). Attachments are base64-decoded and inlined as text blocks. |
| `tools` | ignored | AG-UI lets the client declare available tools; ACP agents own their own tool set, so this is dropped. |
| `state` | `session/set_mode` / `session/set_config_option` (prompt- and resume-time) | `state.mode` / `state.model` / `state.configOptions` are the persisted, always-resent replacement for the `forwardedProps` equivalents (see below). Diffed against the bridge's last-applied baseline so unchanged state resent every run is a no-op. Other `state` keys are ignored (opaque client-owned domain data). |
| `context` | ignored | No ACP equivalent. |
| `forwardedProps.mcpServers` | `session/resume` `mcp_servers` | The AG-UI `{name: {type, url?, command?, …}}` dict is coerced into ACP's `McpServer` schema: the dict key fills `name`, and `headers` defaults to `[]` for http/sse servers (ACP requires both). Anything already conforming passes through unchanged. (The old `forwardedProps.mode` / `.model` / `.configOptions` path was removed in favour of the `state` channel — see the `state` row below.) |
| `resume[].interruptId` | resolves parked Future keyed by the same id | The id is `=== ACP tool_call_id === AG-UI toolCallId`. One correlation key, three names. |
| `resume[].status="resolved"` + `payload` | `AllowedOutcome{optionId: payload, outcome:"selected"}` | The `payload` may be a string, a `{optionId}` dict, or null (defaults to `"once"`). The accepted shape (bare string from the `responseSchema` `enum`) is formally declared on the `Interrupt` itself; the `{optionId}` dict is a bridge-side compatibility fallback, not in the schema. **Not 1:1:** the AG-UI payload is normalised to ACP's `optionId` field. |
| `resume[].status="cancelled"` | `DeniedOutcome{outcome:"cancelled"}` | AG-UI "cancelled" → ACP "cancelled". |

---

## ACP → AG-UI (agent to client)

### `session/update` variants → AG-UI events

| ACP update | AG-UI event(s) | Notes |
|---|---|---|
| `AgentMessageChunk` (text delta) | first → `TEXT_MESSAGE_START` + `TEXT_MESSAGE_CONTENT`; subsequent → `TEXT_MESSAGE_CONTENT` only | **Not 1:1:** ACP has only text deltas — no start/end markers. The bridge **synthesises** `START` on the first delta and `END` when a tool call begins or the turn ends. **State held:** `_current_message_id`, `_has_open_message`. |
| `ToolCallStart` | (buffered — no immediate event) | **Not 1:1:** ACP agents (e.g. opencode) send the tool name (`"bash"`) at `ToolCallStart` time but the actual arguments (command, path, etc.) in a subsequent `ToolCallProgress`. The bridge buffers the tool call and defers `TOOL_CALL_START` until the first `ToolCallProgress` with `raw_input` arrives, so the displayed name can include a key argument (e.g. `"bash: ls -la"`, `"read: foo.ts"`). AG-UI has no tool-call-name-update event, so the name can't be changed once `TOOL_CALL_START` is emitted. The `raw_input` from `ToolCallStart` (if any) is stored as a fallback for args + display name when no `ToolCallProgress.raw_input` arrives before completion. **State held:** `_pending_tool_starts: dict[str, tuple[str, str|None, Any]]` maps `tool_call_id → (tool_name, parent_message_id, start_raw_input)`. |
| `ToolCallProgress` (`status=in_progress`, with `raw_input`) | `TOOL_CALL_START` (flushed from buffer, with display name) + `TOOL_CALL_ARGS` (delta = full `raw_input` as one JSON string) | The actual tool arguments (command, path, etc.) arrive here. The bridge flushes the deferred `TOOL_CALL_START` with `toolCallName = "{name}: {arg}"` derived from the first available key in `raw_input`: `command` (bash-style → `"bash: ls -la"`) or `filePath`/`filepath`/`path` (read/edit/write → `"read: foo.ts"`), then emits `TOOL_CALL_ARGS` in a single delta. Args contain only `raw_input` — no `kind`/`locations` from `ToolCallStart` — keeping live and replay JSON consistent. Emitted exactly once per tool call — **state held:** `_tool_args_emitted: set[str]` guards against duplicate emission. |
| `ToolCallProgress` (`status=running`, with `raw_output`, no `raw_input`) | `TOOL_CALL_ARGS` (delta = `{"_progress": raw_output}`) | ACP carries intermediate output under the same `tool_call_update` kind; the bridge repurposes `TOOL_CALL_ARGS` to surface progress (AG-UI has no "tool progress" event). Only emitted when no `raw_input` is present (the args-vs-progress distinction). |
| `ToolCallProgress` (`status=completed`/`failed`) | `TOOL_CALL_END` **and** `TOOL_CALL_RESULT` | **1:2 split:** one ACP completion → two AG-UI events. `TOOL_CALL_END` only signals end-of-args-streaming; `TOOL_CALL_RESULT` (with `role="tool"`) is what CopilotKit's runtime listens for to synthesise a `ToolMessage` and flip the renderer from `inProgress` to `complete`. Without both, the renderer hangs. |
| `CurrentModeUpdate` | `CUSTOM` (`name="agent:mode_update"`) | Renamed. Also refreshes `ActiveSession.current_mode_id` (via the bridge's `on_mode_changed` callback) so the next run's `state.mode` diff doesn't fight an autonomous agent-side mode change. (Mode itself is deferred to a follow-up STATE_DELTA pass.) |
| `AvailableCommandsUpdate` | `CUSTOM` (`name="agent:commands_available"`) | Renamed. |
| `NewSessionResponse.modes` / `LoadSessionResponse.modes` | `STATE_SNAPSHOT` (`{modes, currentModeId}`) emitted once after `start_run` | **State held:** the modes are read out of the session-create response and stashed on `ActiveSession.modes`, then emitted as a snapshot *after* the run's queue is attached (emitting earlier drops them). |
| **ACP 0.11:** `NewSessionResponse.configOptions` | `STATE_SNAPSHOT` (`{configOptions}`) | Read out of the session-create response and stashed on `ActiveSession.config_options`, then emitted in the same post-`start_run` snapshot as `modes`. Each option is serialised to `{id, name, description?, category?, currentValue, type, options?}`; `_meta` is dropped. |
| **ACP 0.11:** `ConfigOptionUpdate` | `STATE_DELTA` (`[{op:"replace", path:"/configOptions", value:[…]}]`) | The notification carries the full set, so this is a replace of the `/configOptions` path — but as a JSON Patch `STATE_DELTA`, not a partial `STATE_SNAPSHOT`, so it doesn't wipe the `plans`/`usage`/`sessionInfo` the client is also holding (the "STATE_SNAPSHOT merge trap"). Also refreshes `ActiveSession.config_options` (via `on_config_options_changed`) so the next run's `state.configOptions` diff is stable against agent-driven changes. |
| **ACP 0.11:** `UsageUpdate` | `STATE_DELTA` (`[{op:"replace", path:"/usage", value:{used, size, cost?}}]`) | `cost` (when present) is `{amount, currency}`. A running counter — exactly what AG-UI `state` is for (not a fire-and-forget `CUSTOM`). **State held:** `_usage` on the bridge, so the run-start `STATE_SNAPSHOT` baseline carries the current value (persists across runs within a session and survives reconnect via the replay accumulator). |
| **ACP 0.11:** `SessionInfoUpdate` | `STATE_DELTA` (`[{op:"replace", path:"/sessionInfo", value:{title, updatedAt}}]`) | A title/timestamp pair with explicit "set to null to clear" semantics — a current value, not a notification. `null` `title`/`updatedAt` are preserved (not dropped) so the "cleared" signal reaches the client. **State held:** `_session_info` on the bridge. |
| **ACP 0.11:** `AgentPlanUpdate` | `STATE_DELTA` (`[{op:"replace", path:"/plans/default", value:{type:"items", entries:[…]}}]`) | Legacy single-plan full-replace. Stored under the sentinel key `"default"` (no ACP-provided id). `STATE_DELTA`/`STATE_SNAPSHOT` semantics already — ACP's own docstring says "the client replaces the entire plan with each update." **State held:** `_plans["default"]` on the bridge. |
| **ACP 0.11:** `AgentPlanContentUpdate` | `STATE_DELTA` (`[{op:"replace", path:"/plans/<id>", value:{…}}]`) | Newer multi-plan variant; keyed by the plan's `id`. The value mirrors the discriminated union (`items`/`file`/`markdown`), not assuming a checklist. **State held:** `_plans[id]`. |
| **ACP 0.11:** `AgentPlanRemovedUpdate` | `STATE_DELTA` (`[{op:"remove", path:"/plans/<id>"}]`) | Deletes one plan by id. **State held:** pops `_plans[id]`. |
| **ACP 0.11:** `AgentThoughtChunk` | **Live:** `REASONING_START` → `REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` (×N) → `REASONING_MESSAGE_END` → `REASONING_END` **Replay:** `SnapshotMessage{role:"reasoning"}` in the `MESSAGES_SNAPSHOT` | Agent reasoning streamed as thought deltas, mapped to AG-UI's first-class reasoning event family. The bridge synthesises the phase/message framing: a contiguous run of thought chunks opens one reasoning phase with one reasoning message (multiple `CONTENT` deltas), closed on the same lifecycle triggers that close an open text message (tool call start, text message start, turn end, run finish/error). There is only one implementation of these sequencing rules (the live one); during replay the same `REASONING_*` events fold into the `_replay_accumulator` via `_emit()`, producing `role="reasoning"` `SnapshotMessage`s in transcript order (`reasoning` is already one of the variants in AG-UI's upstream `Message` discriminated union — `ReasoningMessageSchema` — so this is not a bridge-local schema extension). |
| `UserMessageChunk` | (dropped in live mode) | In live mode: echo of the user's own message; not needed (AG-UI client already has it). **During replay** (connect): coalesced into the `MESSAGES_SNAPSHOT` as a `role="user"` message (see next row). |
| **replay** (any `session/update` during `session/load`) | `STATE_SNAPSHOT` (state) **then** `MESSAGES_SNAPSHOT` (messages) — one of each, in that order | The entire historical `session/update` stream delivered during `session/load` is coalesced. For messages: agent/user text → `SnapshotMessage{role, content}`, tool calls → `SnapshotMessage{role:"assistant", toolCalls}`, tool results → `SnapshotMessage{role:"tool", toolCallId}`. For state: `STATE_DELTA`/`STATE_SNAPSHOT` events fold into the accumulator's `_state` dict (JSON Patch, RFC 6902 — lenient like the reference client's `fast-json-patch`), then `end_replay()` emits one `STATE_SNAPSHOT` carrying the merged plan/usage/sessionInfo/configOptions. This is what fixes the reconnect bug where plan/usage/session_info used to vanish (they were `CUSTOM` fire-and-forget events the catch-all silently dropped). Framed by a synthetic `RUN_STARTED`/`RUN_FINISHED` pair; the `STATE_SNAPSHOT` precedes the `MESSAGES_SNAPSHOT` so the client applies the state baseline before rendering the transcript. |

### Permission flow (the big impedance mismatch)

| ACP side | AG-UI side | Notes |
|---|---|---|
| `session/request_permission` (a **blocking RPC** the agent calls mid-prompt) | `RUN_FINISHED{outcome:{type:"interrupt", interrupts:[…]}}` then SSE stream **closes** | **Inverted control flow.** ACP blocks; AG-UI ends the run. The bridge reconciles this by parking an `asyncio.Future` and suspending the prompt task at `await future`. The `Interrupt.responseSchema` is set to a `{type:"string", enum:[optionId, …]}` built from the ACP `PermissionOption` ids, so a generic AG-UI client can render the choice (zipped against `metadata.options` for labels/kind) from the wire event alone. **State held:** `_permission_futures: {call_id → Future}`, `_permission_timers: {call_id → TimerHandle}`. |
| (prompt task parked, no ACP traffic) | new `POST /ag-ui` with `resume[]` | The client decides; the bridge routes the resume entry to `resolve_interrupt(call_id, …)` which sets the Future's result, waking the parked prompt task. |
| `AllowedOutcome` / `DeniedOutcome` returned from `request_permission` | (no separate event) | The outcome is folded into the resumed run's event stream — the prompt task continues emitting into the *new* SSE stream. **State held:** `attach_resume_queue` swaps `_queue` to the new run's queue **without** clearing `_open_tool_calls` (the tool call that triggered the permission is still open and continues across the suspend boundary). |
| (no resume within `PERMISSION_TTL_SECONDS`) | (no event) | **State held:** a `loop.call_later` TTL timer resolves the Future as `cancelled` so the prompt task unwinds instead of leaking the subprocess. The same deadline is published as `Interrupt.expiresAt` so the client's own expiry guard agrees with the server's. |

### Extensions

| ACP | AG-UI | Notes |
|---|---|---|
| `ext_notification` with `_kiro.dev/*` or `_session/terminate` | `CUSTOM` (`name` from a hardcoded rename table) | **Renames:** `_kiro.dev/metadata` → `agent:metadata`, `_kiro.dev/mcp/server_initialized` → `agent:mcp_initialized`, `_kiro.dev/compaction/status` → `agent:compaction`, `_kiro.dev/commands/available` → `agent:commands_available`, `_session/terminate` → `agent:subagent_terminated`. Unknown `_*.dev/*` methods get a synthesised `agent:<tail>` name. |
| `ext_notification` arriving **before any run** | buffered, flushed as `CUSTOM` on the first `start_run` / `attach_resume_queue` | **State held:** `_pending_notifications: list[(method, params)]`. Without this, session-init notifications (e.g. `_kiro.dev/mcp/server_initialized` during startup) would be lost — there is no SSE stream to emit them on yet. |
| `ext_method` (vendor request/response) | `{}` (empty dict) | The bridge returns an empty result for every vendor extension *request*; only *notifications* are surfaced. |
| **ACP 0.11:** `create_elicitation` | `RUN_FINISHED{outcome:{type:"interrupt", interrupts:[{reason:"elicitation", responseSchema, metadata:{mode, elicitationId}}]}}` then SSE stream **closes** | Reuses the same suspend/resume plumbing as `request_permission`: a Future keyed by elicitation id (taken from the request for URL mode, or bridge-generated for form mode) is parked, the run ends with an interrupt, and the client resumes with a payload describing accept/decline/cancel. The `Interrupt.responseSchema` carries the ACP `ElicitationSchema` so the client can render a form. |
| **ACP 0.11:** `complete_elicitation` | `CUSTOM` (`name="agent:elicitation_complete"`, `value={elicitationId}`) | Mid-stream completion notification; rare (usually the accept/decline reply closes the loop). |
| **ACP 0.11:** `resume[].payload` for an elicitation | `AcceptElicitationResponse` / `DeclineElicitationResponse` / `CancelElicitationResponse` | `resolve_interrupt` dispatches by id across both the permission and elicitation Future tables. Payload shapes: `{status:"accepted", values:{…}}` → accept with content; `{status:"declined"}` → decline; resume `status="cancelled"` → cancel. |

### File / terminal callbacks (agent → bridge)

| ACP | AG-UI | Notes |
|---|---|---|
| `read_text_file`, `write_text_file` | (no AG-UI event) | **Not implemented.** The bridge advertises `clientCapabilities.fs.readTextFile=false` / `writeTextFile=false` and provides no callbacks, so the ACP SDK router raises `method_not_found` (JSON-RPC -32601) if the agent calls them — the agent must do its own filesystem I/O. Honest "unsupported" behaviour, consistent with the advertised capability. Both the `fs/*` and terminal surfaces are removed entirely in ACP v2. |
| `create_terminal`, `terminal_output`, `release_terminal`, `wait_for_terminal_exit`, `kill_terminal` | (no AG-UI event) | **Stubbed.** The bridge fabricates a terminal id / empty output so the SDK's (non-optional) terminal routes resolve instead of erroring; they never become AG-UI events — invisible to the frontend. (Removed entirely in ACP v2.) |

---

## Config & model discovery

| Direction | Mechanism | Status |
|---|---|---|
| Server → client (advertise options) | `STATE_SNAPSHOT` with `modes` / `models` / `currentModeId` / `configOptions` / `plans` / `usage` / `sessionInfo` | Read at Create time from `session/new`'s response, stashed on `ActiveSession`, then emitted as a snapshot after `start_run` attaches the run's queue. `plans`/`usage`/`sessionInfo` are bridge-tracked evolving state (persist across runs within a session, included even when empty so the paths the mid-turn `STATE_DELTA` `replace` ops touch exist from the first run). Re-emitted on `ConfigOptionUpdate` notifications mid-turn (as a `STATE_DELTA`, not a snapshot). |
| Client → server (select option at create) | `POST /ag-ui/sessions` body (`mode`, `model`, `configOptions`) → `session/set_mode` / `set_config_option` | Applied once at Create, before the first prompt. |
| Mid-session config change | `POST /ag-ui` with `state.mode` / `.model` / `.configOptions` → `session/set_mode` / `session/set_config_option` | `state` is AG-UI's native persisted channel (set once via `agent.setState()`, resent on every run, fresh prompt or resume; round-trips through CopilotKit's `useCoAgent({ state, setState })`). The bridge reads only those three keys; other `state` keys are ignored as opaque client-owned data. Because `state` is resent even when unchanged, the bridge diffs against its last-applied baseline (`ActiveSession.current_mode_id`, the `currentValue` of each advertised `configOptions` entry) and only fires `set_*` for actual changes — a successful apply refreshes the baseline so a re-sent identical `state` is a no-op. Applied on both the fresh-prompt path (after `start_run`, before the post-`start_run` `STATE_SNAPSHOT`) and the resume path (after `resolve_interrupt`). Best-effort — a bad option is logged and skipped, never aborting the run. (The earlier `forwardedProps.mode` / `.model` / `.configOptions` path was removed in favour of `state`; `forwardedProps` now carries only `mcpServers`.) |

---

## Synthetic IDs and renames (summary)

| AG-UI name | ACP name | Relationship |
|---|---|---|
| `runId` | (none) | bridge-generated UUID, rotated per AG-UI run (so a suspended/resumed turn spans 2+ run ids) |
| `threadId` | `session_id` | **equal** — one id, collapsed from the old two-id (`taskId` vs `agent_session_id`) split. The same value keys the `ActiveSession`, the durable `cwd` store, and every ACP session-scoped call. |
| `TOOL_CALL_START.toolCallId` | `ToolCallStart.tool_call_id` | equal |
| `resume[].interruptId` | `request_permission` call_id | **equal** — the single correlation key, also reused as `Interrupt.id` and `Interrupt.toolCallId` |
| `resume[].payload` | `AllowedOutcome.optionId` | normalised (string / `{optionId}` / null → `"once"`) |
| `resume[].status="cancelled"` | `DeniedOutcome.outcome="cancelled"` | literal rename |
| `Custom.name="agent:metadata"` | `ext_notification` method `_kiro.dev/metadata` | hardcoded rename table |

---

## State held on the proxy (load-bearing)

The bridge maintains **mutable state fields** across three scopes. The root cause is three structural mismatches between ACP and AG-UI:

1. **ACP is frameless, AG-UI is framed.** ACP sends delta chunks without start/end markers. AG-UI requires `START`/`CONTENT`/`END` framing. The bridge synthesises framing from arrival order, which requires tracking what's "open."
2. **ACP splits tool args across two events, AG-UI appends.** ACP sends partial `raw_input` at `ToolCallStart`, full `raw_input` at `ToolCallProgress`. AG-UI's `TOOL_CALL_ARGS` is append-only. The bridge must defer args emission and deduplicate.
3. **ACP has no interrupt/resume, AG-UI does.** ACP's `request_permission` is a blocking async call. AG-UI models it as `RUN_FINISHED{outcome:interrupt}` + a later resume run. The bridge parks futures and correlates interrupt IDs.

Replay is **not** a second state machine. The live state machine is the only implementation of the sequencing rules; during `session/load` replay `_emit()` folds each event into a single `MessageSnapshotAccumulator` instead of putting it on the SSE queue, and `end_replay()` emits one `MESSAGES_SNAPSHOT` from the accumulator. A future sequencing fix in the live handlers automatically applies to replay too, since there is nothing replay-specific left to keep in sync. See `proposals/converge-live-replay-state-machine.md` for the design.

| State | Scope | Why it's needed |
|---|---|---|
| **Live streaming** (reset per run) | | |
| `_current_message_id`, `_has_open_message` | per run | ACP has no message start/end; the bridge synthesises `TEXT_MESSAGE_START`/`END` framing. |
| `_current_reasoning_id`, `_has_open_reasoning` | per run | Same framing synthesis for `REASONING_*` events. Closed on text-message start, tool-call start, turn end — so thoughts interleave correctly with text and tool calls (not concatenated into one phase). |
| `_open_tool_calls: set[str]` | per run (preserved across suspend/resume) | Tracks which tool calls still need `TOOL_CALL_END`/`RESULT`. Cleared on `start_run`, **preserved** on `attach_resume_queue` (the tool call that triggered a permission is still open across the suspend boundary). |
| `_tool_args_emitted: set[str]` | per run | Guards against emitting `TOOL_CALL_ARGS` more than once per tool call. opencode sends multiple `ToolCallProgress` updates with `raw_input`; the ag-ui client appends deltas, so duplicate emission concatenates into broken JSON. |
| **Replay** (reset per connect) | | |
| `_replay_accumulator: MessageSnapshotAccumulator \| None` | per connect run | Folds the same AG-UI wire events the live state machine would have emitted into a `list[SnapshotMessage]`; `end_replay()` emits one `MESSAGES_SNAPSHOT` from it. The single fold point — there is no parallel replay state machine. (`UserMessageChunk` is the one retained live/replay behavioural difference: AG-UI's `TextMessageStartEvent.role` is hardcoded `"assistant"`, so the bridge calls `_replay_accumulator.add_user_text` directly during replay and drops the chunk in live mode — a deliberate product decision, not a duplicated sequencing rule.) |
| **Session-level** (persists across runs) | | |
| `ActiveSession` (`session_id`, `cwd`, `runner`, `protocol`, `bridge`, `modes`, `models`, `current_mode_id`, `config_options`, `last_active_at`) | per session, in-memory | The agent subprocess must persist across runs on the same thread. |
| `SessionStore` (`session_id → cwd`, on disk) | durable, survives restart | Written at Create so Connect/Prompt can resolve `cwd` without the client resending it; removed on Delete. |
| `SessionManager._capabilities` | process lifetime (cached after first probe) | The bridge needs to know what the agent supports (load/resume/list/delete) before any session is created. |
| `_pending_notifications: list[(method, params)]` | session-level, drained on first run | Buffers `ext_notification`s that arrive before any SSE stream exists. |
| `_permission_futures: {call_id → Future}` | per parked permission | Bridges ACP's blocking `request_permission` to AG-UI's end-then-resume flow. |
| `_permission_timers: {call_id → TimerHandle}` | per parked permission | Server-side TTL cleanup so a never-resumed permission doesn't leak the subprocess. |
| `_elicitation_futures: {id → Future}` | per parked elicitation | Same suspend/resume plumbing as permissions, for ACP 0.11 `create_elicitation`. |
| `_elicitation_timers: {id → TimerHandle}` | per parked elicitation | TTL cleanup for elicitations. |
| `_queue`, `_run_id` | per AG-UI run | The SSE stream the bridge emits into; swapped on `attach_resume_queue`. |

---

## What AG-UI has that ACP doesn't (and vice versa)

### Implemented

| Concept | In AG-UI? | In ACP? | Notes |
|---|---|---|---|
| Streaming text deltas | ✅ `TEXT_MESSAGE_*` | ✅ `AgentMessageChunk` | maps (with synthesised framing) |
| Streaming tool args | ✅ `TOOL_CALL_ARGS` (append-only delta string) | ✅ `ToolCallStart.raw_input` (partial) + `ToolCallProgress.raw_input` (full) | **Not 1:1:** ACP splits args across two events; AG-UI appends. Bridge defers to `ToolCallProgress` and emits once (guarded by `_tool_args_emitted`). |
| Tool result | ✅ `TOOL_CALL_RESULT` | ✅ `ToolCallProgress.raw_output` | maps (renamed field) |
| Tool progress (in-flight output) | ❌ (repurposes `TOOL_CALL_ARGS`) | ✅ `ToolCallProgress` w/ `status=running` | **not 1:1** |
| Agent reasoning / "thought" | ✅ `REASONING_*` (live) / `role="reasoning"` in `MESSAGES_SNAPSHOT` (replay) | ✅ `AgentThoughtChunk` | **Not 1:1:** Live path synthesises `REASONING_START`/`MESSAGE_START`/`CONTENT`/`END` framing from arrival order; replay coalesces into `role="reasoning"` `SnapshotMessage`s in transcript order. Both paths close reasoning at the same boundaries (text start, tool start, turn end) so thinking interleaves correctly with text and tool calls in both views. |
| Plans / todos | ✅ `STATE_DELTA` (`/plans/<id>`) | ✅ `AgentPlanUpdate` / `…ContentUpdate` / `…RemovedUpdate` | maps (each variant → a JSON Patch op on `/plans/<id>`; legacy no-id plan under the `"default"` sentinel). Survives reconnect via the replay accumulator. |
| Token usage / cost | ✅ `STATE_DELTA` (`/usage`) | ✅ `UsageUpdate` | maps (`{used, size, cost?}` — a running counter, state not notification) |
| Session title / metadata | ✅ `STATE_DELTA` (`/sessionInfo`) | ✅ `SessionInfoUpdate` | maps (with null-clear semantics preserved) |
| Structured user prompts (elicitation) | ✅ `interrupt{reason:"elicitation"}` | ✅ `create_elicitation` (0.11) | maps (reuses the permission suspend/resume plumbing) |
| Tool approval (HITL) | ✅ `RUN_FINISHED{interrupt}` + `resume` | ✅ `request_permission` | maps (with state held — the hard part) |
| Modes | ✅ `STATE_SNAPSHOT.modes` | ✅ `NewSessionResponse.modes` / `CurrentModeUpdate` | maps |
| Models / config options | ✅ `STATE_SNAPSHOT.configOptions` | ✅ `configOptions` (0.11) | maps |
| Mid-session config change | ✅ `POST /ag-ui` `state` | ✅ `set_mode` / `set_config_option` | maps (mode/model/configOptions carried in the `state` channel on the standard run call, diffed against the last-applied baseline) |
| Cancel | ⚠️ client disconnect | ✅ `session/cancel` | maps via disconnect detection |
| File reads/writes by agent | (invisible to client) | ❌ (not implemented) | agent must do its own fs I/O — the bridge advertises `readTextFile=false` / `writeTextFile=false` and the SDK returns `method_not_found` if called |
| Terminals | (invisible to client) | ✅ terminal methods | bridge fabricates ids |
| Session list | ✅ `GET /ag-ui/sessions` | ✅ `session/list` | maps |
| Session delete | ✅ `DELETE /ag-ui/sessions/{id}` | ✅ `session/delete` | maps |
| Session resume (after bridge restart) | ✅ `POST /ag-ui` (attach-only) | ✅ `session/resume` | maps — the bridge resolves `cwd` from its durable store, calls `session/resume`, then `session/prompt` |
| Transcript replay (connect to existing conversation) | ✅ `STATE_SNAPSHOT` + `MESSAGES_SNAPSHOT` (bridge ext: `GET .../connect`) | ✅ `session/load` | maps — the bridge coalesces the historical `session/update` stream into one `STATE_SNAPSHOT` (folded state) then one `MESSAGES_SNAPSHOT` |
| Capability discovery | ✅ `GET /ag-ui/capabilities` | ✅ `InitializeResponse.agentCapabilities` | maps — the bridge probes the agent once and caches |

### Implementable but not currently implemented

| Concept | In AG-UI? | In ACP? | Notes |
|---|---|---|---|
| Session fork | ❌ (no standard surface) | ✅ `session/fork` | ACP defines it; AG-UI has no "fork this thread" operation. The bridge could expose it as a bridge extension (`POST /ag-ui/sessions/{id}/fork`) returning a new `sessionId`, but no client today asks for it. Low value unless a "branch this conversation" UI feature lands. |
| Session close | ❌ (no standard surface) | ✅ `session/close` | ACP's close frees in-flight agent resources without touching the persisted record. This bridge's teardown is `runner.kill()` (process death already frees everything close would), so there's no functional gap — the bridge's own `stop()` / idle-reaper / connect-disconnect path all kill the subprocess. A typed `close_session` call ahead of kill would let the agent flush final metadata (e.g. opencode writing `updatedAt` to SQLite) — a real but minor benefit. Not implemented. |
| `additionalDirectories` | ❌ (not surfaced) | ✅ `session/new` / `load` / `resume` param | ACP lets an agent access files outside `cwd` via additional directories. The bridge accepts `mcpServers` but not `additionalDirectories` on Create. Plumbing it through the `POST /ag-ui/sessions` body would be a one-line addition; left out because no client sends it yet. |
| Cursor-based list pagination | ✅ (`nextCursor` returned) | ✅ `session/list` `cursor` | The endpoint returns `nextCursor` and passes `cursor` through; full pagination is wired but no client exercises it beyond the first page yet. |

### Would need minor extensions to ACP or AG-UI

| Concept | Gap | What it'd take |
|---|---|---|
| Session title settable from the client | AG-UI has no "rename thread" operation; ACP has `SessionInfoUpdate` (agent → client only) | An ACP method for the client to set `SessionInfo.title`, or a bridge extension endpoint that the agent would need a way to receive (no such ACP call exists today). |
| Per-session MCP server add/remove mid-conversation | AG-UI has no standard for this; ACP takes `mcpServers` only at `session/new`/`load`/`resume` | An ACP `session/set_mcp_servers` (doesn't exist) or a full re-`resume` with a new server list (works today but is heavy). |
| Tool progress as a first-class event | AG-UI has no `TOOL_CALL_PROGRESS`; the bridge repurposes `TOOL_CALL_ARGS` with `{"_progress": …}` | An AG-UI spec addition for in-flight tool output, or a `CUSTOM` event with a stable name. |

### Out of scope (doesn't make sense for this bridge)

| Concept | Why not |
|---|---|
| Editor-grade features (`document/*`, `nes/*`, `providers/*`, `authenticate`, `logout`) | These assume a long-lived editor session with open documents. The AG-UI client is a chat UI with nothing to do with them. The bridge's `ext_method` returns `{}` for unknown methods and the SDK marks `document_*` / `nes_*` as optional routes. Explicit non-goal. |
| Multiple sessions multiplexed on one ACP subprocess | ACP is designed for this (one process, one `Map<sessionId, Info>`); this bridge deliberately runs one subprocess per session for simplicity. Reusing one connection across many sessions is a deeper architectural change (tracked in `agui_on_acp_changes.md` §4.3 "broader context") — out of scope for the current model. |
