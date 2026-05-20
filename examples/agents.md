# Agent Configuration Examples

## How It Works

The `agentCommand` field in `bridge.config.json` tells the bridge which binary to spawn. The binary must:

1. Accept communication via stdin/stdout (JSON-RPC 2.0, newline-delimited JSON)
2. Implement the ACP protocol (at minimum: `initialize`, `session/new`, `session/prompt`)
3. Send `session/update` notifications with streaming content

## Configuration Examples

### Kiro CLI

```json
{
  "agentCommand": ["kiro-cli", "acp"]
}
```

Kiro CLI is Amazon's coding agent. Install via [kiro.dev](https://kiro.dev).

### Claude Code

```json
{
  "agentCommand": ["claude", "code", "--acp"]
}
```

Note: Claude Code ACP support may require a specific version. Check [Anthropic's docs](https://docs.anthropic.com) for availability.

### Codex CLI

```json
{
  "agentCommand": ["codex", "--acp"]
}
```

OpenAI's Codex CLI with ACP transport enabled.

### Gemini CLI

```json
{
  "agentCommand": ["gemini", "cli", "acp"]
}
```

Google's Gemini CLI agent.

### Cursor

```json
{
  "agentCommand": ["cursor", "--acp"]
}
```

### OpenCode

```json
{
  "agentCommand": ["opencode", "acp"]
}
```

SST's open-source coding agent.

### Cline

```json
{
  "agentCommand": ["cline", "--acp"]
}
```

### GitHub Copilot CLI

```json
{
  "agentCommand": ["github-copilot-cli", "--acp"]
}
```

Note: Currently in public preview.

### Custom Agent

Any binary that speaks ACP over stdio:

```json
{
  "agentCommand": ["/path/to/my-agent", "--mode", "acp"]
}
```

## Environment Variables

You can set environment variables for the agent process by modifying the runner's `spawn()` call. The bridge passes through the parent process environment plus:

- `AGENT_LOG_LEVEL` — Set to `debug` for verbose agent logging (when `debug=True`)
- `AGENT_LOG_FILE` — Path for agent log output

## Verifying Your Agent Works

1. Start the bridge: `pnpm dev`
2. Create a task via the UI or REST API
3. Check the backend logs for:
   - `Spawned agent [...] PID=...` — agent started successfully
   - `← notification: session/update` — agent is sending events
4. If you see `Process exited (code=1)`, your agent command is likely wrong

## Minimum ACP Implementation

For an agent to work with this bridge, it must handle these JSON-RPC methods:

**Requests (bridge → agent):**
- `initialize` — respond with protocol version and capabilities
- `session/new` — respond with `{ sessionId: "..." }`
- `session/prompt` — process the prompt, send updates, respond when done

**Notifications (agent → bridge):**
- `session/update` — with `sessionUpdate` field: `agent_message_chunk`, `tool_call`, `tool_call_update`, `turn_end`

**Optional but recommended:**
- `session/request_permission` — request tool approval from the user
- `session/cancel` — handle cancellation gracefully
- `session/set_mode` — support mode switching

See the full ACP spec at [agentclientprotocol.com](https://agentclientprotocol.com).
