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
agui-on-acp --agent-command opencode --agent-command acp --port 8000
```

The bridge is now serving:

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | Health check |
| `/ag-ui` | POST | Standard AG-UI run endpoint (SSE response) |
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
[`docs/architecture.md`](docs/architecture.md) for the full interrupt/resume
flow.

## Configuration

All configuration is supplied via CLI flags — **no config file is required**.
The legacy `bridge.config.json` file is still supported as a baseline: values
for fields you don't pass on the command line are read from it (default path
`bridge.config.json`, override with `--config PATH` or the
`AGUI_ON_ACP_CONFIG_PATH` env var).

```text
Usage: agui-on-acp [OPTIONS]

Options:
  --agent-command TOKEN        Command (+ args) used to spawn the ACP agent.
                               Repeat the flag for multi-token commands, e.g.
                               `--agent-command opencode --agent-command acp`.
                               Tokens may also be comma-separated.
                               Default: kiro-cli acp.
  --port INTEGER               Port the bridge listens on.  [default: 8000]
  --host TEXT                  Host the bridge binds to.  [default: 0.0.0.0]
  --cors-origin ORIGIN         Allowed CORS origin. Repeat for multiple.
                               Default: http://localhost:5173, http://localhost:3000.
  --project-name TEXT          Internal project id (used in /health responses).
  --display-title TEXT         Title shown in OpenAPI docs.
  --description TEXT           Description shown in OpenAPI docs.
  --config FILE                Path to a bridge.config.json baseline.
                               [default: bridge.config.json]
  --reload / --no-reload       Restart on source changes (uvicorn --reload).
  --log-level [critical|error|warning|info|debug|trace]
                               Uvicorn log level.  [default: info]
  -h, --help                   Show this message and exit.
```

### Examples

Run with opencode as the agent on port 9000:

```bash
agui-on-acp --agent-command opencode --agent-command acp --port 9000 --no-reload
```

Allow an extra CORS origin (in addition to defaults from a config file):

```bash
agui-on-acp --agent-command opencode --agent-command acp \
            --cors-origin http://localhost:8080
```

Run against a custom ACP binary:

```bash
agui-on-acp --agent-command ./bin/my-agent --agent-command acp --port 8000
```

### `bridge.config.json` (optional)

If you prefer a file, drop a `bridge.config.json` next to the binary:

```json
{
  "projectName": "my-project",
  "displayTitle": "My AG-UI Bridge",
  "description": "Custom ACP → AG-UI bridge",
  "agentCommand": ["opencode", "acp"],
  "backendPort": 8000,
  "corsOrigins": ["http://localhost:5173", "http://localhost:3000"]
}
```

CLI flags override matching fields in the file; fields you omit on the CLI
fall through to the file (and then to defaults).

> The bridge keeps **all session state in memory** — there is no database.
> Restarting the process loses active sessions; AG-UI clients should start
> a fresh `threadId` after a bridge restart.

## Development

```bash
./check.sh     # type-check (pyright)
./format.sh    # isort + black
pytest         # run the suite
```

See [`docs/architecture.md`](docs/architecture.md) for the component layout
and event-flow diagrams.
