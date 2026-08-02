# Summary of implementation

## Code health

Enter the venv, then run all of:

- ./format.sh
- pyright
- ./lint.sh
- pytest

## Exposed APIS

- The AGUI API
- An extra "sessions" API for CRUD ops on sessions, which can then be interacted with via the AGUI API.

The three session-lifecycle operations are deliberately split.

- `POST /ag-ui/sessions` — Create (`session/new`). The only endpoint that
  starts a fresh conversation.
- `GET /ag-ui/sessions/{id}/connect` — Connect (`session/load`). Replays
  the session's history as a `MESSAGES_SNAPSHOT`.
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

Mode/model/config changes mid-conversation are applied via AG-UI's native
`state` channel (`RunAgentInput.state`), read on `POST /ag-ui` on both the
fresh-prompt and the resume path. `state` is persisted client-side (e.g.
CopilotKit's `useCoAgent({ state, setState })`) and resent on every run —
fresh prompt or resume — so a setting changed once sticks without every call
site having to re-supply it. (The earlier `forwardedProps.mode` / `.model` /
`.configOptions` path was removed in favour of `state`; `forwardedProps`
now carries only `mcpServers`, a create/resume-time parameter.)

Because `state` is resent even when unchanged, the bridge diffs the incoming
`mode` / `model` / `configOptions` against its last-applied baseline
(`ActiveSession.current_mode_id` and the `currentValue` of each advertised
`configOptions` entry) and only fires `session/set_mode` /
`session/set_config_option` for fields that actually differ. A successful
apply refreshes the baseline so the next run that resends the same `state` is
a no-op. The apply runs after `start_run` (fresh path) / `resolve_interrupt`
(resume path) attached the run's queue, before the post-`start_run`
`STATE_SNAPSHOT`, and is best-effort (a bad option is logged and skipped,
never aborting the turn). See `proposals/state-based-session-config.md` for
the full rationale.

For the full bidirectional field mapping and impedance-mismatch notes, see [agui-acp-mapping.md](agui-acp-mapping.md).

## Evolving state: plan / usage / sessionInfo

Beyond config (mode/model/configOptions), the bridge routes three more
ACP update kinds through AG-UI's native `state` channel rather than
`CUSTOM` fire-and-forget events:

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
and session title survive disconnect/reconnect instead of vanishing. See
`proposals/plan-usage-session-info-as-state.md`.

## Persistent State

The backend ACP server holds the majority of the state.  Additional state, per session, is held
in a dir `~/.agui-on-acp` or the alternative configured via the AGUI_ON_ACP_DATA_DIR env var.
That per-session state is a single `session_id → cwd` record (`<data_dir>/sessions/{id}.json`),
written at create time so `connect`/`prompt` can resolve cwd without the client resending it.
This is because ACP requires the original cwd to be provided again when resuming a session.