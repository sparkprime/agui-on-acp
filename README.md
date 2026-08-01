# AG-UI on ACP

A fork of https://github.com/namanrajpal/acp-to-agui/ with changes:

- Just the backend (no UI here)
- pure AG-UI protocol (no side-channel for permissions)
- side channel for session management
- bug fixes
- support for more of ACP and AGUI protocol
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
export AGUI_ON_ACP_ALLOWED_CWD_PREFIXES=/path/to/project
agui-on-acp
```

Or override defaults (see --help for more options):

```bash
export AGUI_ON_ACP_ALLOWED_CWD_PREFIXES=/path/to/project
agui-on-acp --agent-command "opencode acp" --port 8000
```

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

## Configuration

Configuration is supplied via CLI flags **or** environment variables — there
is no config file. The CLI flags just populate the `AGUI_ON_ACP_*`
environment variables for the uvicorn worker, so anything achievable from
the command line is also achievable (and overridable) through the
environment. The env vars (or defaults) are all printed as the server
#starts. See `agui_on_acp/config.py` for the details.

## Developing

See `DESIGN.md`.
