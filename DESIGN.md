# Summary of implementation

## Why this exists

Two protocols exist for two different reasons. ACP is narrow and closed
by design — one domain (coding agent ↔ editor/host), so it can afford a
strongly-typed, closed vocabulary (sessions, permissions, plans, modes,
elicitation, file/terminal ops). AG-UI is broad and open by design — any
agent ↔ any UI, so it *can't* assume domain concepts like "tool
permission" or "coding session" exist; it only standardizes the shape of
the pipe (`TEXT_MESSAGE_*`, `TOOL_CALL_*`, `state`, `CUSTOM`,
`interrupt`/`resume`) and leaves content to each backend.

This project exists to let the ACP coding-agent ecosystem (opencode,
claude-agent-acp, kiro-cli, ...) be consumed by the AG-UI frontend
ecosystem (CopilotKit, generic `HttpAgent` clients, custom apps, generic
AG-UI dev tools). ACP's domain richness is expressed
through AG-UI's own sanctioned mechanisms.

| ACP concept | AG-UI-native encoding |
|---|---|
| Tool permission | `RUN_FINISHED{outcome:interrupt}` with `Interrupt.responseSchema` (a real JSON Schema, same field elicitation uses) |
| Mode / model / config | `RunAgentInput.state` (persisted, round-trips automatically) |
| Plan / usage / session title | `STATE_DELTA` (JSON Patch) on `state.plans` / `.usage` / `.sessionInfo` |
| Structured user prompts | `interrupt{reason:"elicitation"}` reusing the same suspend/resume plumbing as permissions |

## Code health

Enter the venv, then run all of:

- ./format.sh
- pyright
- ./lint.sh
- pytest

## Exposed APIs: the AG-UI run surface, plus a session-bootstrapping envelope

- **The AG-UI API** (`POST /ag-ui`) — the data plane. A run on an
  existing session is standard AG-UI; although ACP-specific *content* rides
  through AG-UI's own extension points (see the table above).
- **A "sessions" envelope** (`POST /ag-ui/sessions`, `GET
  /ag-ui/sessions/{id}/connect`, `GET /ag-ui/sessions`, `DELETE
  /ag-ui/sessions/{id}`, `GET /ag-ui/capabilities`) — the control plane
  that has to run *before* `/ag-ui` can be used at all, because ACP needs
  an explicit process-spawn/session-mint step AG-UI has no concept of.

The three session-lifecycle operations are deliberately split.

- `POST /ag-ui/sessions` — Create (`session/new`). The only endpoint that
  starts a fresh conversation.
- `GET /ag-ui/sessions/{id}/connect` — Connect (`session/load`). Replays
  the session's history as a `STATE_SNAPSHOT` (evolving state:
  plan/usage/sessionInfo/configOptions) then a `MESSAGES_SNAPSHOT`.
- `POST /ag-ui` — Prompt. Attach-only: it reuses a live session or calls
  `session/resume`.

## Error model

Pre-stream failures — anything rejected before an SSE stream opens
(unknown session id, unsupported capability, cwd not allowed, no user
message) — return plain JSON `{"error": "..."}` with an appropriate HTTP
status code (400/403/404/409/501). No `text/event-stream` content type,
no `RUN_ERROR` event; a non-SSE content type immediately signals "this
isn't a stream."

Mid-stream failures — errors that occur after a 200 +
`text/event-stream` has started (e.g. the agent's `prompt()` raises
mid-turn) — are surfaced as `RUN_ERROR` SSE events on the already-opened
stream, where the client is already committed to parsing SSE.

## Mapping

AGUI "thread id" is 1:1 with ACP "session id". There is some impedence mismatch where we have to store partially complete ACP interactions while waiting for AGUI interactions that will complete them.

### Permission flow

Tool-approval permission requests (`session/request_permission`) surface
as `RUN_FINISHED{outcome:{type:"interrupt", interrupts:[{reason:"tool_call", ...}]}}`.
`Interrupt.responseSchema` is populated with `{type:"string",
enum:[optionId, ...]}` built from the ACP `PermissionOption` list — the
same AG-UI field elicitation uses. `metadata.options` is
kept alongside for human-readable labels/kind, but a client that only
reads `responseSchema` still gets a valid, if unlabeled, choice.

### Mode/model/config via native `state`

Mode/model/config changes mid-conversation are applied via AG-UI's native
`state` channel (`RunAgentInput.state`), read on `POST /ag-ui` on both the
fresh-prompt and the resume path. `state` is persisted client-side (e.g.
CopilotKit's `useCoAgent({ state, setState })`) and resent on every run —
fresh prompt or resume — so a setting changed once sticks without every call
site having to re-supply it.

Because `state` is resent even when unchanged, the bridge diffs the incoming
`mode` / `model` / `configOptions` against its last-applied baseline
(`ActiveSession.current_mode_id` and the `currentValue` of each advertised
`configOptions` entry) and only fires `session/set_mode` /
`session/set_config_option` for fields that actually differ. A successful
apply refreshes the baseline so the next run that resends the same `state` is
a no-op. The apply runs after `start_run` (fresh path) / `resolve_interrupt`
(resume path) attached the run's queue, before the post-`start_run`
`STATE_SNAPSHOT`, and is best-effort (a bad option is logged and skipped,
never aborting the turn). Firing immediately, with no gating on the agent
being "idle," is intentional and spec-sanctioned — ACP explicitly documents
`session/set_mode`/`session/set_config_option` as callable "at any time...
whether the Agent is idle or actively generating a response," and the
underlying JSON-RPC transport multiplexes concurrent bidirectional requests
by id rather than serializing behind a lock, so there's no protocol or
transport reason to wait.

For the full bidirectional field mapping and impedance-mismatch notes, see [agui-acp-mapping.md](agui-acp-mapping.md).

## Evolving state: plan / usage / sessionInfo

Beyond config (mode/model/configOptions), the bridge routes three more
ACP update kinds through AG-UI's native `state` channel:

- `AgentPlanUpdate` / `AgentPlanContentUpdate` / `AgentPlanRemovedUpdate` →
  `STATE_DELTA` JSON Patch ops on `/plans/<id>` (legacy no-id plan under the
  `"default"` sentinel).
- `UsageUpdate` → `STATE_DELTA` `replace /usage` (a running token/cost
  counter).
- `SessionInfoUpdate` → `STATE_DELTA` `replace /sessionInfo` (title +
  timestamp, with null-clear semantics preserved).
- `ConfigOptionUpdate` → `STATE_DELTA` `replace /configOptions` (was a
  partial `STATE_SNAPSHOT`; moved to a delta so it no longer wipes
  plan/usage/sessionInfo — the "STATE_SNAPSHOT merge trap").

The bridge tracks the current values (`_plans` / `_usage` / `_session_info`,
persistent across runs within a session) and includes them in the
post-`start_run` `STATE_SNAPSHOT` baseline, so the first per-field
`STATE_DELTA` `replace` is valid under a strict RFC 6902 applier. A
`CurrentModeUpdate` (still a `CUSTOM` event for now) likewise refreshes
`ActiveSession.current_mode_id` via an `on_mode_changed` callback so the
next run's `state.mode` diff doesn't fight an autonomous agent-side mode
change; `ConfigOptionUpdate` does the same for `active.config_options` via
`on_config_options_changed`.

This also fixes a reconnect bug: during `session/load` replay the
`MessageSnapshotAccumulator` folds `STATE_SNAPSHOT`/`STATE_DELTA` into a
parallel `_state` dict (a lenient RFC 6902 applier, like the reference
client's `fast-json-patch`) and `end_replay()` emits one `STATE_SNAPSHOT`
from it alongside the `MESSAGES_SNAPSHOT` — so the todo list, token meter,
and session title survive disconnect/reconnect instead of vanishing.

## Hydrating state at attach time

`modes` / `configOptions` / `currentModeId` are extracted from the ACP
attach response on all three lifecycle paths — `create_session`
(`NewSessionResponse`), `connect_session` (`LoadSessionResponse`), and
`attach_for_prompt` (`ResumeSessionResponse`) — via `_extract_session_meta`.
On `connect_session` the extracted meta is also injected into the replay
accumulator (`bridge.merge_replay_state`) so the connect `STATE_SNAPSHOT`
carries the full current state, not just the folded plan/usage/sessionInfo.
Without this, the mode selector and config UI started empty after a
connect/resume (the bug: only `create_session` extracted meta; the replay
snapshot and the first prompt's `STATE_SNAPSHOT` carried no modes).

`plans` / `usage` / `sessionInfo` are recoverable on `connect_session`
(folded from the replayed `session/update` stream) but **not** on
`attach_for_prompt` — `session/resume` deliberately doesn't replay the
transcript, and ACP's `ResumeSessionResponse` carries only
`modes`/`configOptions`, never plan/usage/sessionInfo. So after a bridge
restart those three start empty until the agent re-emits a fresh update.

## Persistent State

The backend ACP server holds the majority of the state.  Additional state, per session, is held
in a dir `~/.agui-on-acp` or the alternative configured via the AGUI_ON_ACP_DATA_DIR env var.
That per-session state is a single `session_id → cwd` record (`<data_dir>/sessions/{id}.json`),
written at create time so `connect`/`prompt` can resolve cwd without the client resending it.
This is because ACP requires the original cwd to be provided again when resuming a session.
