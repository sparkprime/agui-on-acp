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

Mode/model/config changes mid-conversation are applied via
`forwardedProps.mode` / `.model` / `.configOptions` on `POST /ag-ui` —
AG-UI's sanctioned extension field (`RunAgentInput.forwardedProps` is
untyped by design). The bridge applies them after `start_run` attaches the
run's queue and before the post-`start_run` `STATE_SNAPSHOT`, best-effort
(a bad option is logged and skipped, never aborting the turn). This
replaces an earlier bridge-only `POST /ag-ui/config` endpoint; there is no
bespoke sibling endpoint for config changes — use the standard run call.

For the full bidirectional field mapping and impedance-mismatch notes, see [agui-acp-mapping.md](agui-acp-mapping.md).

## Persistent State

The backend ACP server holds the majority of the state.  Additional state, per session, is held
in a dir `~/.agui-on-acp` or the alternative configured via the AGUI_ON_ACP_DATA_DIR env var.
That per-session state is a single `session_id → cwd` record (`<data_dir>/sessions/{id}.json`),
written at create time so `connect`/`prompt` can resolve cwd without the client resending it.
This is because ACP requires the original cwd to be provided again when resuming a session.