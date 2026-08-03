# AG-UI on ACP

`agui-on-acp` is an [AG-UI](https://docs.ag-ui.com/) server for
[Agent Client Protocol](https://agentclientprotocol.com) (ACP) coding
agents. It is not a translator sitting between two peer protocols — the
`/ag-ui` run endpoint is a genuine, spec-compliant AG-UI server. ACP's
domain-specific concepts (tool permissions, plans, token/cost usage,
modes, elicitation) are expressed through AG-UI's own sanctioned
extension points (`state` / `STATE_DELTA`, `responseSchema`, `CUSTOM`),
not through private conventions bolted on top of the wire format. Any
AG-UI client — CopilotKit, `HttpAgent`, a custom React frontend, or a
generic AG-UI dev tool that only understands the spec — can talk to it
with no bridge-specific glue code for the run itself.

The one thing that genuinely *isn't* AG-UI is kept honestly separate
rather than smuggled into an AG-UI extension point: ACP's process/session
model needs an explicit bootstrapping step (spawn a subprocess, mint a
session id, bind a `cwd`) that AG-UI's run contract has no concept of —
`RunAgentInput.threadId` is just an opaque id presumed to already refer
to something. That bootstrapping is exposed as a small "sessions"
envelope (`POST /ag-ui/sessions`, `GET .../connect`, etc.) sitting in
front of the `/ag-ui` run endpoint. See `DESIGN.md` for why this split
exists and what would break if it didn't.

Originally forked from https://github.com/namanrajpal/acp-to-agui/, since
diverged substantially:

- Just the backend (no UI here)
- AG-UI-native throughout — no private side channels for permission
  approval, mode/model/config changes, or plan/usage/session-info; each
  rides through the AG-UI mechanism actually designed for it (see
  `agui-acp-mapping.md`)
- an explicit, separate session-management envelope (create / connect /
  list / delete), rather than folding ACP's session lifecycle into the
  run endpoint itself
- bug fixes
- support for more of ACP and AG-UI protocol
- tested with opencode and CopilotKit

The bridge spawns your chosen ACP agent as a subprocess per session and
translates its JSON-RPC notifications into AG-UI SSE events.

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

### 4. Create a session, then point an AG-UI client at it

`POST /ag-ui` is attach-only — it never creates a session. Create one
first via the sessions envelope, then use the returned `sessionId` as the
AG-UI `threadId` on every subsequent `POST /ag-ui` run:

```bash
# 1. Create a session (once per conversation) — this is the only call
#    that spawns the agent subprocess and decides `cwd`.
curl -s -X POST http://localhost:8000/ag-ui/sessions \
  -H "Content-Type: application/json" \
  -d '{"cwd": "/path/to/project"}'
# => {"sessionId": "...", "modes": [...], "configOptions": [...], ...}

# 2. Run a prompt on it — this IS the standard AG-UI run endpoint, with
#    the session id as threadId. No forwardedProps.cwd, no side channel.
curl -N -X POST http://localhost:8000/ag-ui \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "threadId": "<sessionId from step 1>",
    "messages": [{"role": "user", "content": "List the files in this repo."}]
  }'
```

With the CopilotKit `HttpAgent`, the same two steps look like:

```ts
const { sessionId } = await fetch("http://localhost:8000/ag-ui/sessions", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ cwd: "/path/to/project" }),
}).then((r) => r.json());

const agent = new HttpAgent({
  url: "http://localhost:8000/ag-ui",
  threadId: sessionId,
});
```

You'll receive an SSE stream of AG-UI events (`RUN_STARTED`,
`TEXT_MESSAGE_CONTENT`, `TOOL_CALL_START`, `STATE_SNAPSHOT`,
`RUN_FINISHED`, etc.). When the agent requests a tool approval, the run
finishes with an `interrupt` outcome (its `responseSchema` describes the
choice, so a generic AG-UI client can render it without knowing this is
ACP underneath) and you resume it by POSTing again with a `resume` array
— see [`agui-acp-mapping.md`](agui-acp-mapping.md) for the full
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
