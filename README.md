# AG-UI on ACP

A fork of https://github.com/namanrajpal/acp-to-agui/

- Just the backend (no UI here)
- pure AG-UI protocol (no side-channel)
- some bug fixes
- tested with opencode and CopilotKit

`agui-on-acp` is a protocol translator that exposes any
[Agent Client Protocol](https://agentclientprotocol.com) (ACP) compatible
coding agent over the standard
[AG-UI](https://docs.ag-ui.com/) event stream. Any AG-UI client (CopilotKit,
`HttpAgent`, custom React frontends) can talk to it; the bridge spawns your
chosen ACP agent as a subprocess and translates the JSON-RPC notifications
into AG-UI SSE events.

## Quick start

### 1. Install

```bash
cd agui-on-acp
uv sync            # or: python -m venv .venv && source .venv/bin/activate && uv pip install -e .
```

This installs the `agui-on-acp` console script.

### 2. Have an ACP agent on your PATH

The bridge spawns whatever agent command you give it. The most common choice
is [opencode](https://opencode.ai):

```bash
opencode --version             # make sure it's installed
opencode acp --help            # confirm the `acp` subcommand is available
```

Any other ACP-compatible binary works too (`kiro-cli acp`,
`claude-agent-acp`, a custom one, etc.).

### 3. Start the bridge

```bash
agui-on-acp --agent-command "opencode acp" --port 8000
```

The bridge is now serving:

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check |
| `/ag-ui` | POST | Standard AG-UI run endpoint (SSE response) |
| `/ag-ui/config` | POST | Bridge extension: apply mid-session config options (`{threadId, configOptions}`) without starting a run |
| `/docs` | GET | Interactive OpenAPI UI |

### 4. Point an AG-UI client at it

From any AG-UI client, POST a `RunAgentInput` to `http://localhost:8000/ag-ui`
with `Accept: text/event-stream`. For example, with the CopilotKit
`HttpAgent`:

```ts
const agent = new HttpAgent({
  url: "http://localhost:8000/ag-ui",
});
```

Or with `curl`:

```bash
curl -N -X POST http://localhost:8000/ag-ui \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "threadId": "demo",
    "messages": [{"role": "user", "content": "List the files in this repo."}],
    "forwardedProps": { "cwd": "." }
  }'
```

You'll receive an SSE stream of AG-UI events (`RUN_STARTED`,
`TEXT_MESSAGE_CONTENT`, `TOOL_CALL_START`, `RUN_FINISHED`, etc.). When the
agent requests a tool approval, the run finishes with an `interrupt` outcome
and you resume it by POSTing again with a `resume` array — see
[`docs/agui-acp-mapping.md`](docs/agui-acp-mapping.md) for the full
interrupt/resume flow.

## How it works

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Python Backend (FastAPI)                            │
│                        localhost:8000                                   │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                    AG-UI Endpoint                                  │ │
│  │  POST /ag-ui              — Standard AG-UI run (fresh + resume)    │ │
│  │  GET  /health             — Liveness probe                         │ │
│  └───────────────────────────┬────────────────────────────────────────┘ │
│                              │                                          │
│  ┌───────────────────────────┼───────────────────────────────────────┐  │
│  │                     SessionManager                                │  │
│  │  ┌──────────────────────┐  ┌──────────────────────────────────┐   │  │
│  │  │ AgentRunner          │  │ AcpToAguiBridge                  │   │  │
│  │  │ (ACP proc)           │  │ (event translator)               │   │  │
│  │  └──────────────────────┘  └──────────────────────────────────┘   │  │
│  │                                                                   │  │
│  │                ┌──────────┴───────────┐                           │  │
│  │                │ AcpProtocol          │                           │  │
│  │                │ (JSON-RPC interface) │                           │  │
│  │                └──────────┬───────────┘                           │  │
│  └───────────────────────────┼───────────────────────────────────────┘  │
└──────────────────────────────┼──────────────────────────────────────────┘
                               │
                               │ stdin/stdout (JSON-RPC 2.0 / ndjson)
                               ▼
                    ┌──────────────────────┐
                    │   ACP Agent          │
                    │   (subprocess)       │
                    └──────────────────────┘
```

All session state is held in memory by `SessionManager` — there is no
database. Restarting the process loses active sessions; AG-UI clients
should start a fresh `threadId` after a bridge restart.

### Components

| Module | Path | Description |
|--------|------|-------------|
| Main | `agui_on_acp/main.py` | FastAPI app, lifespan, router setup |
| Config | `agui_on_acp/config.py` | Env-var-backed config accessors (`AGUI_ON_ACP_*`) |
| Agent Runner | `agui_on_acp/agent/runner.py` | Subprocess management (parameterized command) |
| ACP Protocol | `agui_on_acp/agent/acp_protocol.py` | Typed JSON-RPC interface |
| Bridge | `agui_on_acp/bridge/acp_to_agui.py` | ACP notification → AG-UI event translator (interrupt/resume HITL) |
| AG-UI Events | `agui_on_acp/agui/events.py` | Pydantic event type models (incl. Interrupt, InterruptOutcome) |
| SSE Encoder | `agui_on_acp/agui/sse.py` | SSE stream encoding (cancel-on-disconnect) |
| Session Manager | `agui_on_acp/sessions/manager.py` | In-memory session lifecycle (start_run, resume_run, cancel_run) |
| AG-UI Endpoint | `agui_on_acp/agui_endpoint.py` | Standard POST /ag-ui (fresh + resume routing) |

See [`docs/agui-acp-mapping.md`](docs/agui-acp-mapping.md) for the full
AG-UI ↔ ACP field-by-field mapping, including where the translation is not
1:1, where the proxy holds state, and the ACP 0.11 gaps.

## Codebase overview

```
agui_on_acp/
├── __main__.py        # Click CLI entry point (the `agui-on-acp` binary)
├── main.py            # FastAPI app, lifespan, router wiring
├── config.py          # Env-var-backed config accessors (AGUI_ON_ACP_*)
├── logging_config.py  # Colored demo formatter (◀ ACP / ● BRIDGE / ▶ AG-UI)
├── agui_endpoint.py   # POST /ag-ui — the standard AG-UI run endpoint
├── sessions/
│   └── manager.py     # SessionManager — in-memory session table + run lifecycle
├── agent/
│   ├── runner.py      # AgentRunner — spawns/kills the ACP subprocess via the SDK
│   └── acp_protocol.py# AcpProtocol — typed wrapper over the SDK's connection methods
├── bridge/
│   └── acp_to_agui.py # AcpToAguiBridge — the translator (acp.Client impl)
└── agui/
    ├── events.py      # Pydantic models for every AG-UI event type
    └── sse.py         # SSE encoder + cancel-on-disconnect drain
```

The request path through these, for a fresh `POST /ag-ui`:

1. **`agui_endpoint.py`** parses `RunAgentInput`, looks up `threadId` in the
   manager's in-memory table, and on a miss calls `manager.create_task(...)`
   (which spawns the agent) before `manager.start_run(...)` (which sends the
   prompt). The response is an SSE `StreamingResponse` draining the run's
   `asyncio.Queue`.
2. **`sessions/manager.py`** owns the `task_id → ActiveSession` map and the
   suspend/resume/cancel state machine. It creates an `AcpToAguiBridge` per
   session, spawns the agent via `AgentRunner`, and drives `AcpProtocol`
   calls (`session/new`, `session/prompt`, `session/cancel`, `set_mode`,
   `set_config_option`).
3. **`agent/runner.py`** wraps `acp.spawn_agent_process` — process spawn,
   Windows-shim handling, kill-the-tree on shutdown. **`agent/acp_protocol.py`**
   is a thin logging layer over the SDK's `ClientSideConnection` methods.
4. **`bridge/acp_to_agui.py`** is the heart. It implements the `acp.Client`
   Protocol so the SDK routes every `session_update` / `request_permission`
   / `ext_notification` / file / terminal callback here. It holds the
   per-run state (open message id, open tool-call set, parked permission
   Futures, pre-run notification buffer) and emits AG-UI events into the
   run's queue.
5. **`agui/events.py`** defines the Pydantic models for every event on the
   wire; **`agui/sse.py`** drains the queue into SSE chunks, terminates on
   `RUN_FINISHED`/`RUN_ERROR`, and turns a client disconnect into
   `manager.cancel_run` via the `on_cancel` callback.

The two "edge" files — `agui_endpoint.py` (AG-UI side) and
`agent/acp_protocol.py` (ACP side) — are deliberately thin; almost all
logic lives in `sessions/manager.py` (orchestration) and
`bridge/acp_to_agui.py` (translation + state). `main.py` just wires the
lifespan and CORS; `config.py` and `__main__.py` are the run/config
surface.

## Configuration

Configuration is supplied via CLI flags **or** environment variables — there
is no config file. The CLI flags just populate the `AGUI_ON_ACP_*`
environment variables for the uvicorn worker, so anything achievable from
the command line is also achievable (and overridable) through the
environment. See `agui_on_acp/config.py` for the canonical accessor
functions.

| Env var | Type | Default | Description |
|---------|------|---------|-------------|
| `AGUI_ON_ACP_PROJECT_NAME` | str | `acp-to-agui` | Internal project id (used in `/health`). |
| `AGUI_ON_ACP_DISPLAY_TITLE` | str | `ACP → AG-UI Bridge` | Title shown in OpenAPI docs. |
| `AGUI_ON_ACP_DESCRIPTION` | str | `Give any ACP-compatible coding agent a rich web UI` | Description shown in OpenAPI docs. |
| `AGUI_ON_ACP_AGENT_COMMAND` | str (shell-style) | `opencode acp` | Command (+ args) used to spawn the ACP agent, parsed with `shlex.split`. |
| `AGUI_ON_ACP_BACKEND_PORT` | int | `8000` | Port the bridge listens on. |
| `AGUI_ON_ACP_CORS_ORIGINS` | list[str] (comma-separated) | `http://localhost:5173,http://localhost:3000,http://localhost:4200` | Allowed CORS origins. |

Any `AGUI_ON_ACP_*` variable not in the table above is unrecognised and is
flagged with a warning at startup (`config.validate_env_vars`). The
effective configuration is logged on startup via
`config.log_the_config`.

```text
Usage: agui-on-acp [OPTIONS]

Options:
  --agent-command COMMAND      Command (and args) used to spawn the ACP agent, as
                               a single shell-style string, e.g.
                               `--agent-command "opencode acp"`.
                               Default: opencode acp.
  --port INTEGER               Port the bridge listens on.  [default: 8000]
  --host TEXT                  Host the bridge binds to.  [default: 0.0.0.0]
  --cors-origin ORIGIN         Allowed CORS origin. Repeat for multiple.
                               Default: http://localhost:5173, http://localhost:3000, http://localhost:4200.
  --project-name TEXT          Internal project id (used in /health responses).
  --display-title TEXT         Title shown in OpenAPI docs.
  --description TEXT           Description shown in OpenAPI docs.
  --reload / --no-reload       Restart on source changes (uvicorn --reload).
  --log-level [critical|error|warning|info|debug|trace]
                               Uvicorn log level.  [default: info]
  -h, --help                   Show this message and exit.
```

### Examples

Run with opencode as the agent on port 9000:

```bash
agui-on-acp --agent-command "opencode acp" --port 9000 --no-reload
```

Allow an extra CORS origin (in addition to the defaults):

```bash
agui-on-acp --agent-command "opencode acp" --cors-origin http://localhost:8080
```

Run against a custom ACP binary:

```bash
agui-on-acp --agent-command "./bin/my-agent acp" --port 8000
```

Configure entirely through environment variables (no CLI flags needed):

```bash
AGUI_ON_ACP_AGENT_COMMAND="opencode acp" \
AGUI_ON_ACP_BACKEND_PORT=9000 \
AGUI_ON_ACP_CORS_ORIGINS=http://localhost:8080 \
agui-on-acp --no-reload
```

> The bridge keeps **all session state in memory** — there is no database.
> Restarting the process loses active sessions; AG-UI clients should start
> a fresh `threadId` after a bridge restart.

## Development

```bash
./check.sh     # type-check (pyright)
./format.sh    # isort + black
pytest         # run the suite
```
