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
| `state` | ignored | No ACP equivalent. |
| `context` | ignored | No ACP equivalent. |
| `forwardedProps.mcpServers` | `session/resume` `mcp_servers` | The AG-UI `{name: {type, url?, command?, …}}` dict is coerced into ACP's `McpServer` schema: the dict key fills `name`, and `headers` defaults to `[]` for http/sse servers (ACP requires both). Anything already conforming passes through unchanged. |
| `forwardedProps.mode` / `.model` / `.configOptions` | `session/set_mode` / `session/set_config_option` (prompt-time) | Applied on `POST /ag-ui` *after* `start_run` attaches the run's queue and *before* the post-`start_run` `STATE_SNAPSHOT` — this is the sanctioned mid-conversation way to change mode/model/config (the bridge-only `POST /ag-ui/config` endpoint was removed in favour of it). Field names match the create-time typed fields on `POST /ag-ui/sessions` for consistency; application is best-effort (a bad option is logged and skipped, never aborting the run). See "Config & model discovery" below. |
| `resume[].interruptId` | resolves parked Future keyed by the same id | The id is `=== ACP tool_call_id === AG-UI toolCallId`. One correlation key, three names. |
| `resume[].status="resolved"` + `payload` | `AllowedOutcome{optionId: payload, outcome:"selected"}` | The `payload` may be a string, a `{optionId}` dict, or null (defaults to `"once"`). **Not 1:1:** the AG-UI payload is normalised to ACP's `optionId` field. |
| `resume[].status="cancelled"` | `DeniedOutcome{outcome:"cancelled"}` | AG-UI "cancelled" → ACP "cancelled". |

---

## ACP → AG-UI (agent to client)

### `session/update` variants → AG-UI events

| ACP update | AG-UI event(s) | Notes |
|---|---|---|
| `AgentMessageChunk` (text delta) | first → `TEXT_MESSAGE_START` + `TEXT_MESSAGE_CONTENT`; subsequent → `TEXT_MESSAGE_CONTENT` only | **Not 1:1:** ACP has only text deltas — no start/end markers. The bridge **synthesises** `START` on the first delta and `END` when a tool call begins or the turn ends. **State held:** `_current_message_id`, `_has_open_message`. |
| `ToolCallStart` | `TOOL_CALL_START` + `TOOL_CALL_ARGS` (with full args as one JSON delta) | ACP splits "start" from "args"; AG-UI emits both back-to-back. **Not 1:1:** opencode's ACP impl doesn't populate `raw_input` for read/glob/bash — only `kind` and `locations`. The bridge enriches the args delta with `kind`/`locations` so the renderer isn't a blank `{}`. |
| `ToolCallProgress` (`status=running`, with `raw_output`) | `TOOL_CALL_ARGS` (delta = `{"_progress": raw_output}`) | ACP carries intermediate output under the same `tool_call_update` kind; the bridge repurposes `TOOL_CALL_ARGS` to surface progress (AG-UI has no "tool progress" event). |
| `ToolCallProgress` (`status=completed`/`failed`) | `TOOL_CALL_END` **and** `TOOL_CALL_RESULT` | **1:2 split:** one ACP completion → two AG-UI events. `TOOL_CALL_END` only signals end-of-args-streaming; `TOOL_CALL_RESULT` (with `role="tool"`) is what CopilotKit's runtime listens for to synthesise a `ToolMessage` and flip the renderer from `inProgress` to `complete`. Without both, the renderer hangs. |
| `CurrentModeUpdate` | `CUSTOM` (`name="agent:mode_update"`) | Renamed. |
| `AvailableCommandsUpdate` | `CUSTOM` (`name="agent:commands_available"`) | Renamed. |
| `NewSessionResponse.modes` / `LoadSessionResponse.modes` | `STATE_SNAPSHOT` (`{modes, currentModeId}`) emitted once after `start_run` | **State held:** the modes are read out of the session-create response and stashed on `ActiveSession.modes`, then emitted as a snapshot *after* the run's queue is attached (emitting earlier drops them). |
| **ACP 0.11:** `NewSessionResponse.configOptions` | `STATE_SNAPSHOT` (`{configOptions}`) | Read out of the session-create response and stashed on `ActiveSession.config_options`, then emitted in the same post-`start_run` snapshot as `modes`. Each option is serialised to `{id, name, description?, category?, currentValue, type, options?}`; `_meta` is dropped. |
| **ACP 0.11:** `ConfigOptionUpdate` | `STATE_SNAPSHOT` (`{configOptions}`) | The notification carries the full set, so this is a replace not a patch — a fresh `STATE_SNAPSHOT` is emitted on every update. |
| **ACP 0.11:** `UsageUpdate` | `CUSTOM` (`name="agent:usage"`, `value={used, size, cost?}`) | `cost` (when present) is `{amount, currency}`. Clients render a token/cost meter; dedupe upstream. |
| **ACP 0.11:** `SessionInfoUpdate` | `CUSTOM` (`name="agent:session_info"`, `value={title?, updatedAt?}`) | Carries `title` and `updatedAt`; useful for titling the conversation thread. |
| **ACP 0.11:** `AgentPlanUpdate` / `AgentPlanContentUpdate` / `AgentPlanRemovedUpdate` | `CUSTOM` (`agent:plan` / `agent:plan_update` / `agent:plan_removed`) | Each variant maps to a `CUSTOM` whose `value` is the plan payload verbatim (`{entries}` / the discriminated plan content / `{id}`). Clients that don't render plans ignore them. |
| **ACP 0.11:** `AgentThoughtChunk` | `REASONING_START` → `REASONING_MESSAGE_START` → `REASONING_MESSAGE_CONTENT` (×N) → `REASONING_MESSAGE_END` → `REASONING_END` | Agent reasoning streamed as thought deltas, now mapped to AG-UI's first-class reasoning event family (was a `CUSTOM agent:thought` escape hatch). The bridge synthesises the phase/message framing: a contiguous run of thought chunks opens one reasoning phase with one reasoning message (multiple `CONTENT` deltas), closed on the same lifecycle triggers that close an open text message (tool call start, turn end, run finish/error). The text-message stream is kept clean so clients decide whether to surface reasoning. Reasoning is **not** folded into the `MESSAGES_SNAPSHOT` on `connect` replay (spec-conformant — AG-UI permits omitting reasoning from snapshots); historical thought chunks emit live during replay. |
| `UserMessageChunk` | (dropped in live mode) | In live mode: echo of the user's own message; not needed (AG-UI client already has it). **During replay** (connect): coalesced into the `MESSAGES_SNAPSHOT` as a `role="user"` message (see next row). |
| **replay** (any `session/update` during `session/load`) | `MESSAGES_SNAPSHOT` (one event) | The entire historical `session/update` stream delivered during `session/load` is coalesced into a single `MESSAGES_SNAPSHOT` event (AG-UI's "replace the whole message list" operation). The bridge redirects its coalescing state machine — agent/user text → `SnapshotMessage{role, content}`, tool calls → `SnapshotMessage{role:"assistant", toolCalls}`, tool results → `SnapshotMessage{role:"tool", toolCallId}` — instead of emitting deltas. Framed by a synthetic `RUN_STARTED`/`RUN_FINISHED` pair so the SSE stream has normal start/end markers. |

### Permission flow (the big impedance mismatch)

| ACP side | AG-UI side | Notes |
|---|---|---|
| `session/request_permission` (a **blocking RPC** the agent calls mid-prompt) | `RUN_FINISHED{outcome:{type:"interrupt", interrupts:[…]}}` then SSE stream **closes** | **Inverted control flow.** ACP blocks; AG-UI ends the run. The bridge reconciles this by parking an `asyncio.Future` and suspending the prompt task at `await future`. **State held:** `_permission_futures: {call_id → Future}`, `_permission_timers: {call_id → TimerHandle}`. |
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
| Server → client (advertise options) | `STATE_SNAPSHOT` with `modes` / `models` / `currentModeId` / `configOptions` | Read at Create time from `session/new`'s response, stashed on `ActiveSession`, then emitted as a snapshot after `start_run` attaches the run's queue. Re-emitted on `ConfigOptionUpdate` notifications mid-turn. |
| Client → server (select option at create) | `POST /ag-ui/sessions` body (`mode`, `model`, `configOptions`) → `session/set_mode` / `set_config_option` | Applied once at Create, before the first prompt. |
| Mid-session config change | `POST /ag-ui` with `forwardedProps.mode` / `.model` / `.configOptions` → `session/set_mode` / `session/set_config_option` | `forwardedProps` is AG-UI's sanctioned extension mechanism (`RunAgentInput.forwardedProps` is untyped `any` by design). The bridge applies these on the prompt call, *after* `start_run` attached the run's queue (so any reflected `session/update` has a live sink) and *before* the post-`start_run` `STATE_SNAPSHOT`. Application is best-effort — a bad option is logged and skipped, never aborting the run (same policy as Create-time application). No bespoke sibling endpoint. |

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

| State | Lifetime | Why it's needed |
|---|---|---|
| `ActiveSession` (`session_id`, `cwd`, `runner`, `protocol`, `bridge`, `modes`, `models`, `current_mode_id`, `config_options`, `last_active_at`) | per session, in-memory | The agent subprocess must persist across runs on the same thread. `session_id` is the single id for both AG-UI and ACP. |
| `SessionStore` (`session_id → cwd`, on disk) | durable, survives restart | Written at Create so Connect/Prompt can resolve `cwd` without the client resending it; removed on Delete. |
| `SessionManager._capabilities` | process lifetime (cached after first probe) | The bridge needs to know what the agent supports (load/resume/list/delete) before any session is created (e.g. `GET /ag-ui/capabilities`). |
| `bridge._current_message_id`, `_has_open_message` | per run | ACP has no message start/end; the bridge synthesises them. |
| `bridge._open_tool_calls: set[str]` | per run (and across suspend/resume) | Tracks which tool calls still need a `TOOL_CALL_END`/`RESULT` either at turn end or on resume. Cleared on `start_run`, **preserved** on `attach_resume_queue`. |
| `bridge._permission_futures: {call_id → Future}` | per parked permission | Bridges ACP's blocking `request_permission` to AG-UI's end-then-resume flow. |
| `bridge._permission_timers: {call_id → TimerHandle}` | per parked permission | Server-side TTL cleanup so a never-resumed permission doesn't leak the subprocess. |
| `bridge._pending_notifications: list[(method, params)]` | session-level, drained on first run | Buffers `ext_notification`s that arrive before any SSE stream exists. |
| `bridge._queue`, `bridge._run_id` | per AG-UI run | The SSE stream the bridge emits into; swapped on `attach_resume_queue`. |
| `bridge._replay_messages`, `_replay_open_tools` | per connect (replay) run | Coalesces the historical `session/update` stream into one `MESSAGES_SNAPSHOT`. |

---

## What AG-UI has that ACP doesn't (and vice versa)

### Implemented

| Concept | In AG-UI? | In ACP? | Notes |
|---|---|---|---|
| Streaming text deltas | ✅ `TEXT_MESSAGE_*` | ✅ `AgentMessageChunk` | maps (with synthesised framing) |
| Streaming tool args | ✅ `TOOL_CALL_ARGS` (delta string) | ✅ `ToolCallStart.raw_input` | maps (one-shot in ACP, chunked in AG-UI) |
| Tool result | ✅ `TOOL_CALL_RESULT` | ✅ `ToolCallProgress.raw_output` | maps (renamed field) |
| Tool progress (in-flight output) | ❌ (repurposes `TOOL_CALL_ARGS`) | ✅ `ToolCallProgress` w/ `status=running` | **not 1:1** |
| Agent reasoning / "thought" | ✅ `REASONING_*` | ✅ `AgentThoughtChunk` | maps (with synthesised phase/message framing — one phase per contiguous run) |
| Plans / todos | `CUSTOM agent:plan[_update|_removed]` | ✅ `AgentPlanUpdate` / `…ContentUpdate` / `…RemovedUpdate` | maps (each variant → a `CUSTOM` with the plan payload) |
| Token usage / cost | `CUSTOM agent:usage` | ✅ `UsageUpdate` | maps (with `{used, size, cost?}`) |
| Session title / metadata | `CUSTOM agent:session_info` | ✅ `SessionInfoUpdate` | maps |
| Structured user prompts (elicitation) | ✅ `interrupt{reason:"elicitation"}` | ✅ `create_elicitation` (0.11) | maps (reuses the permission suspend/resume plumbing) |
| Tool approval (HITL) | ✅ `RUN_FINISHED{interrupt}` + `resume` | ✅ `request_permission` | maps (with state held — the hard part) |
| Modes | ✅ `STATE_SNAPSHOT.modes` | ✅ `NewSessionResponse.modes` / `CurrentModeUpdate` | maps |
| Models / config options | ✅ `STATE_SNAPSHOT.configOptions` | ✅ `configOptions` (0.11) | maps |
| Mid-session config change | ✅ `POST /ag-ui` `forwardedProps` | ✅ `set_mode` / `set_config_option` | maps (mode/model/configOptions carried in `forwardedProps` on the standard run call) |
| Cancel | ⚠️ client disconnect | ✅ `session/cancel` | maps via disconnect detection |
| File reads/writes by agent | (invisible to client) | ❌ (not implemented) | agent must do its own fs I/O — the bridge advertises `readTextFile=false` / `writeTextFile=false` and the SDK returns `method_not_found` if called |
| Terminals | (invisible to client) | ✅ terminal methods | bridge fabricates ids |
| Session list | ✅ `GET /ag-ui/sessions` | ✅ `session/list` | maps |
| Session delete | ✅ `DELETE /ag-ui/sessions/{id}` | ✅ `session/delete` | maps |
| Session resume (after bridge restart) | ✅ `POST /ag-ui` (attach-only) | ✅ `session/resume` | maps — the bridge resolves `cwd` from its durable store, calls `session/resume`, then `session/prompt` |
| Transcript replay (connect to existing conversation) | ✅ `MESSAGES_SNAPSHOT` (bridge ext: `GET .../connect`) | ✅ `session/load` | maps — the bridge coalesces the historical `session/update` stream into one `MESSAGES_SNAPSHOT` |
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
