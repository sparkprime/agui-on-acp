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

- **AG-UI → ACP** rows describe what the client sends (HTTP `POST /ag-ui`
  body fields + SSE-stream lifecycle) and what ACP call the bridge issues.
- **ACP → AG-UI** rows describe what the agent emits and what AG-UI event
  the bridge synthesises.
- **State held** marks where the bridge keeps something in memory to make
  a non-1:1 translation work; these are the load-bearing parts of the proxy.
- **ACP 0.11** tags rows whose mapping changed or whose feature was added
  in `agent-client-protocol` 0.11.

---

## AG-UI → ACP (client to agent)

### Run lifecycle

| AG-UI input | ACP call | Notes |
|---|---|---|
| `POST /ag-ui` (fresh, with `messages`) | `session/new` (or `session/load` if `forwardedProps.resumeSessionId`) then `session/prompt` | The run *is* the prompt. AG-UI has no separate "create session" step — the first run for a `threadId` implicitly spawns the agent and creates the ACP session. **State held:** the bridge keeps an `ActiveSession` keyed by `threadId` so the *second* run on the same thread reuses the spawned subprocess instead of respawning. |
| `POST /ag-ui` with `resume[]` (non-empty) | no new ACP call — resolves a parked `request_permission` Future | The AG-UI client never calls `session/prompt` again on resume; it signals "user decided". The bridge routes the resume entry to `bridge.resolve_permission(interruptId, …)`, which unblocks the *original* `session/prompt` task that was parked mid-turn. **1 ACP turn ↔ N+1 AG-UI runs** when N permission points are hit. |
| Client TCP disconnect mid-SSE | `session/cancel` | AG-UI has no explicit cancel verb. The bridge detects disconnect as `CancelledError` in the SSE drain and calls `manager.cancel_run` → `session/cancel` + resolves any parked permission Futures as `cancelled`. |

### `RunAgentInput` fields → ACP

| AG-UI field | ACP effect | Notes |
|---|---|---|
| `threadId` | ACP `session_id` (indirectly) | The bridge uses `threadId` as its own `task_id`; the ACP `session_id` is a separate UUID returned by `session/new`. They are **not** the same value. **State held:** `ActiveSession.{task_id, agent_session_id}` mapping. |
| `runId` | ignored | AG-UI lets the client propose a run id; the bridge ignores it and generates its own UUID per run (so it can rotate run ids across suspend/resume). |
| `messages[-1].content` | `session/prompt` `prompt[0] = {type:"text", text:…}` | Only the **last** user message is forwarded; AG-UI's full message history is **not** replayed to ACP (the agent keeps its own session history). Attachments are base64-decoded and inlined as text blocks. |
| `tools` | ignored | AG-UI lets the client declare available tools; ACP agents own their own tool set, so this is dropped. |
| `state` | ignored | No ACP equivalent. |
| `context` | ignored | No ACP equivalent. |
| `forwardedProps.cwd` | `session/new` / `session/load` `cwd` | Drives the agent's working directory. |
| `forwardedProps.resumeSessionId` | selects `session/load` vs `session/new` | If set, the bridge calls `session/load` with the given id instead of creating a fresh session. |
| `forwardedProps.mode` | `session/set_mode` (`modeId`) | Issued once, after `session/new`/`load`, before the first prompt. Skipped if the value is the placeholder `"default"`. |
| `forwardedProps.model` | **ACP 0.11:** `session/set_config_option` (`config_id="model"`) | Renamed in 0.11: the model is no longer its own method (`session/set_model` was removed); it is now one config option among many. The bridge hard-codes `config_id="model"`. |
| `forwardedProps.agentCommand` | `AgentRunner` spawn args | Per-request override of the binary spawned (default from `--agent-command`). Only honoured on the *first* run for a thread (the subprocess is already running afterwards). |
| `forwardedProps.mcpServers` | `session/new` / `session/load` `mcp_servers` | The AG-UI `{name: {type, url?, command?, …}}` dict is coerced into ACP's `McpServer` schema: the dict key fills `name`, and `headers` defaults to `[]` for http/sse servers (ACP requires both). Anything already conforming passes through unchanged. |
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
| `NewSessionResponse.models` (legacy) / `.availableModels` | `STATE_SNAPSHOT` (`{models}`) | Same deferred-snapshot pattern. |
| **ACP 0.11:** `NewSessionResponse.configOptions` | `STATE_SNAPSHOT` (`{configOptions}`) | Read out of the session-create response and stashed on `ActiveSession.config_options`, then emitted in the same post-`start_run` snapshot as `modes`/`models`. Each option is serialised to `{id, name, description?, category?, currentValue, type, options?}` (select options carry `{value, name, description?}`); `_meta` is dropped. |
| **ACP 0.11:** `ConfigOptionUpdate` | `STATE_SNAPSHOT` (`{configOptions}`) | The notification carries the full set, so this is a replace not a patch — a fresh `STATE_SNAPSHOT` is emitted on every update. |
| **ACP 0.11:** `UsageUpdate` | `CUSTOM` (`name="agent:usage"`, `value={used, size, cost?}`) | `cost` (when present) is `{amount, currency}`. Clients render a token/cost meter; dedupe upstream. |
| **ACP 0.11:** `SessionInfoUpdate` | `CUSTOM` (`name="agent:session_info"`, `value={title?, updatedAt?}`) | Carries `title` and `updatedAt`; useful for titling the conversation thread. |
| **ACP 0.11:** `AgentPlanUpdate` / `AgentPlanContentUpdate` / `AgentPlanRemovedUpdate` | `CUSTOM` (`agent:plan` / `agent:plan_update` / `agent:plan_removed`) | Each variant maps to a `CUSTOM` whose `value` is the plan payload verbatim (`{entries}` / the discriminated plan content / `{id}`). Clients that don't render plans ignore them. |
| **ACP 0.11:** `AgentThoughtChunk` | `CUSTOM` (`name="agent:thought"`, `value={delta}`) | Agent reasoning streamed as thought deltas; the text message stream is kept clean so clients decide whether to surface reasoning. |
| `UserMessageChunk` | (dropped) | Echo of the user's own message; not needed (AG-UI client already has it). |

### Permission flow (the big impedance mismatch)

| ACP side | AG-UI side | Notes |
|---|---|---|
| `session/request_permission` (a **blocking RPC** the agent calls mid-prompt) | `RUN_FINISHED{outcome:{type:"interrupt", interrupts:[…]}}` then SSE stream **closes** | **Inverted control flow.** ACP blocks; AG-UI ends the run. The bridge reconciles this by parking an `asyncio.Future` and suspending the prompt task at `await future`. **State held:** `_permission_futures: {call_id → Future}`, `_permission_timers: {call_id → TimerHandle}`. |
| (prompt task parked, no ACP traffic) | new `POST /ag-ui` with `resume[]` | The client decides; the bridge routes the resume entry to `resolve_permission(call_id, …)` which sets the Future's result, waking the parked prompt task. |
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
| `read_text_file`, `write_text_file`, `create_terminal`, `terminal_output`, `release_terminal`, `wait_for_terminal_exit`, `kill_terminal` | (no AG-UI event) | These are **server-side callbacks** the bridge handles itself (it reads/writes files under `cwd`, fabricates terminal ids). They never become AG-UI events — they're invisible to the frontend. **State held:** `bridge._cwd` for path resolution. |

---

## Config & model discovery

| Direction | Mechanism | Status |
|---|---|---|
| Server → client (advertise options) | `STATE_SNAPSHOT` with `modes` / `models` / `currentModeId` / `configOptions` | Works for both the legacy `NewSessionResponse.modes`/`.models` fields and the ACP 0.11 `configOptions` field (read at session-create and re-emitted on `ConfigOptionUpdate` notifications). |
| Client → server (select option) | `forwardedProps.model` → `session/set_config_option(config_id="model")`; `forwardedProps.configOptions` (a `{config_id: value}` dict) → `set_config_option` for each at session-create time | Works at session-create time. |
| Mid-session config change | `POST /ag-ui/config` (bridge extension) → `session/set_config_option` per option | AG-UI's `POST /ag-ui` is always a fresh run or a resume; the bridge exposes a separate `POST /ag-ui/config` endpoint (`{threadId, configOptions}`) so clients can switch models / toggle options mid-session without contorting the run contract. Not part of the AG-UI standard. |

---

## Synthetic IDs and renames (summary)

| AG-UI name | ACP name | Relationship |
|---|---|---|
| `runId` | (none) | bridge-generated UUID, rotated per AG-UI run (so a suspended/resumed turn spans 2+ run ids) |
| `taskId` | `session_id` | **not equal** — task_id is the bridge's own key; session_id comes from `session/new` |
| `threadId` | (none) | bridge sets `threadId === taskId` (AG-UI requires it) |
| `TOOL_CALL_START.toolCallId` | `ToolCallStart.tool_call_id` | equal |
| `resume[].interruptId` | `request_permission` call_id | **equal** — the single correlation key, also reused as `Interrupt.id` and `Interrupt.toolCallId` |
| `resume[].payload` | `AllowedOutcome.optionId` | normalised (string / `{optionId}` / null → `"once"`) |
| `resume[].status="cancelled"` | `DeniedOutcome.outcome="cancelled"` | literal rename |
| `Custom.name="agent:metadata"` | `ext_notification` method `_kiro.dev/metadata` | hardcoded rename table |

---

## State held on the proxy (load-bearing)

| State | Lifetime | Why it's needed |
|---|---|---|
| `ActiveSession` (`task_id`, `agent_session_id`, `runner`, `protocol`, `bridge`, `modes`, `models`, `current_mode_id`) | per thread, in-memory only | AG-UI `threadId` ↔ ACP `session_id` are different ids; the agent subprocess must persist across runs on the same thread. |
| `bridge._current_message_id`, `_has_open_message` | per run | ACP has no message start/end; the bridge synthesises them. |
| `bridge._open_tool_calls: set[str]` | per run (and across suspend/resume) | Tracks which tool calls still need a `TOOL_CALL_END`/`RESULT` either at turn end or on resume. Cleared on `start_run`, **preserved** on `attach_resume_queue`. |
| `bridge._permission_futures: {call_id → Future}` | per parked permission | Bridges ACP's blocking `request_permission` to AG-UI's end-then-resume flow. |
| `bridge._permission_timers: {call_id → TimerHandle}` | per parked permission | Server-side TTL cleanup so a never-resumed permission doesn't leak the subprocess. |
| `bridge._pending_notifications: list[(method, params)]` | session-level, drained on first run | Buffers `ext_notification`s that arrive before any SSE stream exists. |
| `bridge._queue`, `bridge._run_id` | per AG-UI run | The SSE stream the bridge emits into; swapped on `attach_resume_queue`. |
| `bridge._cwd` | per session | Resolves relative paths for the `read_text_file` / `write_text_file` callbacks. |
| `SessionManager._sessions: {task_id → ActiveSession}` | process lifetime | The whole session table; lost on restart (the bridge is intentionally stateless across restarts — clients start a fresh `threadId`). |

---

## What AG-UI has that ACP doesn't (and vice versa)

| Concept | In AG-UI? | In ACP? | Notes |
|---|---|---|---|
| Streaming text deltas | ✅ `TEXT_MESSAGE_*` | ✅ `AgentMessageChunk` | maps (with synthesised framing) |
| Streaming tool args | ✅ `TOOL_CALL_ARGS` (delta string) | ✅ `ToolCallStart.raw_input` | maps (one-shot in ACP, chunked in AG-UI) |
| Tool result | ✅ `TOOL_CALL_RESULT` | ✅ `ToolCallProgress.raw_output` | maps (renamed field) |
| Tool progress (in-flight output) | ❌ (repurposes `TOOL_CALL_ARGS`) | ✅ `ToolCallProgress` w/ `status=running` | **not 1:1** |
| Agent reasoning / "thought" | `CUSTOM agent:thought` | ✅ `AgentThoughtChunk` | maps (via `CUSTOM` with `{delta}`) |
| Plans / todos | `CUSTOM agent:plan[_update|_removed]` | ✅ `AgentPlanUpdate` / `…ContentUpdate` / `…RemovedUpdate` | maps (each variant → a `CUSTOM` with the plan payload) |
| Token usage / cost | `CUSTOM agent:usage` | ✅ `UsageUpdate` | maps (with `{used, size, cost?}`) |
| Session title / metadata | `CUSTOM agent:session_info` | ✅ `SessionInfoUpdate` | maps |
| Structured user prompts (elicitation) | ✅ `interrupt{reason:"elicitation"}` | ✅ `create_elicitation` (0.11) | maps (reuses the permission suspend/resume plumbing) |
| Tool approval (HITL) | ✅ `RUN_FINISHED{interrupt}` + `resume` | ✅ `request_permission` | maps (with state held — the hard part) |
| Modes | ✅ `STATE_SNAPSHOT.modes` | ✅ `NewSessionResponse.modes` / `CurrentModeUpdate` | maps |
| Models / config options | ✅ `STATE_SNAPSHOT.models` / `.configOptions` | ✅ `configOptions` (0.11) | maps (legacy `models` + 0.11 `configOptions`) |
| Mid-session config change | ✅ `POST /ag-ui/config` (bridge ext) | ✅ `set_config_option` | maps (bridge extension endpoint) |
| Cancel | ⚠️ client disconnect | ✅ `session/cancel` | maps via disconnect detection |
| Multiple sessions per agent process | ❌ | ✅ `list`/`fork`/`resume`/`close` | **not exposed** (see [§ Multi-session surface](#multi-session-surface)) |
| File reads/writes by agent | (invisible to client) | ✅ `read_text_file` etc. | bridge handles server-side |
| Terminals | (invisible to client) | ✅ terminal methods | bridge fabricates ids |

---

## Out of scope: editor-grade features

ACP 0.11 carries a full editor-integration surface — document sync
(`document/*`), next-edit-suggestions (`nes/*`), and provider/auth
management (`providers/*`, `authenticate`, `logout`). These assume a
long-lived editor session with open documents, and the AG-UI client (a chat
UI) has nothing to do with them. The bridge's `ext_method` returns `{}`
for unknown methods and the SDK marks `document_*` / `nes_*` as optional
routes, so no code change is needed here — this is an explicit non-goal.

## Multi-session surface

ACP lets one agent process hold many sessions; AG-UI's contract is strictly
one `threadId` ↔ one bridge-side `ActiveSession` ↔ one ACP `session_id`.
The additional ACP session ops (`session/fork`, `session/resume`,
`session/list`, `session/close`) are **not exposed** through AG-UI today —
they are documented in [`new_acp.md`](new_acp.md) § P6 as a deferred,
larger surface-area expansion.
