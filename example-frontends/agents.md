# Agent Configuration Examples

This guide shows how to use the ACP → AG-UI bridge with real agents. Each example includes installation, configuration, and verification steps.

---

## Kiro CLI

[Kiro CLI](https://kiro.dev) is Amazon's coding agent with full ACP support.

### Install

Download from [kiro.dev/downloads](https://kiro.dev/downloads/). After installation, verify:

```bash
which kiro-cli
# Typically: ~/.local/bin/kiro-cli
```

### Configure

```json
{
  "agentCommand": ["kiro-cli", "acp"]
}
```

Or with a specific agent configuration:

```json
{
  "agentCommand": ["kiro-cli", "acp", "--agent", "my-agent"]
}
```

### Environment Variables

| Variable | Description |
|----------|-------------|
| `KIRO_LOG_LEVEL` | Set to `debug` for verbose logging |
| `KIRO_CHAT_LOG_FILE` | Custom log file path |

### Session Data

Sessions are stored at `~/.kiro/sessions/cli/` with `.json` metadata and `.jsonl` event logs.

### Verify It Works

```bash
# Start the bridge
pnpm dev

# In another terminal, check the backend logs for:
# "Spawned agent ['kiro-cli', 'acp'] PID=..."
# "← notification: session/update"
```

### ACP Features Supported

- Modes: `default`, `browser-agent`, `architect`, `ask`, custom agents
- Slash commands: advertised via `_kiro.dev/commands/available`
- MCP servers: configured via `mcpServers` in task creation
- Tool approvals: full `session/request_permission` support
- Session resume: via `session/load`

---

## Claude Agent (via claude-agent-acp)

[claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp) is an ACP adapter for the official Claude Agent SDK by Zed Industries. It wraps Anthropic's Claude Agent SDK to speak ACP over stdio.

### Prerequisites

- Node.js 18+
- An Anthropic API key (`ANTHROPIC_API_KEY`)

### Install

```bash
# Option 1: Install globally
npm install -g @agentclientprotocol/claude-agent-acp

# Option 2: Use npx (no install needed)
# The bridge will run: npx @agentclientprotocol/claude-agent-acp

# Option 3: Clone and build from source
git clone https://github.com/agentclientprotocol/claude-agent-acp.git
cd claude-agent-acp
npm install
npm run build
```

### Configure

If installed globally:

```json
{
  "agentCommand": ["claude-agent-acp"]
}
```

If using npx:

```json
{
  "agentCommand": ["npx", "@agentclientprotocol/claude-agent-acp"]
}
```

If built from source:

```json
{
  "agentCommand": ["node", "/path/to/claude-agent-acp/dist/index.js"]
}
```

### Environment Variables

Set your Anthropic API key before starting the bridge:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
pnpm dev
```

Or add it to a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

### ACP Features Supported

- Context @-mentions and images
- Tool calls with permission requests
- Edit review
- TODO lists
- Interactive and background terminals
- Custom slash commands
- Client MCP servers

### Verify It Works

```bash
# Set API key
export ANTHROPIC_API_KEY="sk-ant-..."

# Start the bridge
pnpm dev

# Open http://localhost:5173
# Create a task, send a message
# You should see Claude's streaming response in the chat panel
```

---

## Running the Demo (End-to-End)

Here's the complete flow to go from zero to a working web UI on top of an ACP agent:

### Step 1: Clone and install

```bash
git clone https://github.com/your-username/acp-to-agui.git
cd acp-to-agui
pnpm install
```

### Step 2: Choose your agent

Edit `bridge.config.json`:

```json
{
  "agentCommand": ["claude-agent-acp"]
}
```

### Step 3: Set credentials (if needed)

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

### Step 4: Start

```bash
pnpm dev
```

This starts:
- Backend at `http://localhost:8000` (FastAPI, spawns your agent)
- Frontend at `http://localhost:5173` (React, connects via SSE)

### Step 5: Use it

1. Open `http://localhost:5173`
2. Select a project directory
3. The bridge spawns your agent subprocess and initializes ACP
4. Type a message — it flows through JSON-RPC to your agent
5. The agent's response streams back as AG-UI events
6. Tool calls appear with approval dialogs
7. You have a full web workspace powered by a CLI agent

---

## How This Project Helped

Without this bridge, connecting an ACP agent to a web UI would require:

1. **Subprocess management** — spawning, monitoring, killing the agent process
2. **JSON-RPC implementation** — bidirectional message parsing, request correlation, error handling
3. **Protocol translation** — converting ACP's notification model to frontend-friendly events
4. **State tracking** — open messages, open tool calls, pending approvals
5. **SSE streaming** — encoding events, keepalives, connection lifecycle
6. **Task persistence** — SQLite store for session metadata
7. **Approval flow** — holding RPC IDs, emitting state updates, responding to agent

This project handles all of that. You just:
- Set `agentCommand` to your agent binary
- Build your frontend consuming AG-UI events from the SSE stream
- Or use the reference UI as-is

**Time to first working UI: ~2 minutes** (clone, configure, `pnpm dev`).

---

## Codex CLI (via codex-acp)

[Codex CLI](https://github.com/openai/codex) is OpenAI's coding agent. ACP support is provided through Zed's adapter ([codex-acp](https://github.com/zed-industries/codex-acp)).

### Prerequisites

One of the following authentication methods:

1. **ChatGPT subscription** (Plus, Pro, Team, or Enterprise) — sign in interactively via browser OAuth
2. **`CODEX_API_KEY`** — dedicated Codex API key
3. **`OPENAI_API_KEY`** — standard OpenAI Platform API key

No API key is required if you authenticate with your ChatGPT subscription. The interactive sign-in flow uses your plan's limits rather than usage-based billing.

You only need an API key when:
- Running in remote/headless environments where browser OAuth isn't available
- Using programmatic workflows that can't do interactive login
- Wanting to bypass ChatGPT subscription message limits

### Install

```bash
# Option 1: npm (recommended)
npm install -g @zed-industries/codex-acp

# Option 2: npx (no install needed)
# The bridge will run: npx @zed-industries/codex-acp

# Option 3: Download pre-built binary from GitHub releases
# https://github.com/zed-industries/codex-acp/releases
```

### Configure

If installed globally:

```json
{
  "agentCommand": ["codex-acp"]
}
```

If using npx:

```json
{
  "agentCommand": ["npx", "@zed-industries/codex-acp"]
}
```

### Environment Variables (optional)

Only needed if you cannot use interactive ChatGPT sign-in:

| Variable | Description |
|----------|-------------|
| `CODEX_API_KEY` | Dedicated Codex API key |
| `OPENAI_API_KEY` | Standard OpenAI Platform API key |

### ACP Features Supported

- Context @-mentions and images
- Tool calls with permission requests
- Edit review
- TODO lists
- Following (real-time streaming)
- Client MCP servers
- Slash commands: `/review`, `/review-branch`, `/review-commit`, `/init`, `/compact`, custom prompts

### Verify It Works

```bash
# If using API key:
export CODEX_API_KEY="..."
# Or just start — you'll get a browser sign-in prompt on first use

pnpm dev

# Open http://localhost:5173
# Create a task, send a message
# You should see Codex's streaming response in the chat panel
```

---

## OpenCode

[OpenCode](https://opencode.ai) is an open-source AI coding agent by SST with native ACP support built-in (no adapter needed).

### Prerequisites

One of the following authentication methods:

1. **OpenCode Zen** (recommended for new users) — a pay-as-you-go AI gateway with a single API key that gives access to all curated models (GPT, Claude, Gemini, Qwen, and more). Sign up at [opencode.ai/auth](https://opencode.ai/auth), add billing details, and get your key. Includes free models during beta.
2. **Provider API key** — bring your own key for any supported provider (Anthropic, OpenAI, Google, etc.)

### Install

```bash
# Option 1: Quick install script
curl -fsSL https://opencode.ai/install | bash

# Option 2: npm
npm install -g opencode-ai@latest

# Option 3: Homebrew (macOS/Linux)
brew install anomalyco/tap/opencode

# Option 4: Windows (Scoop)
scoop install opencode
```

### Configure

```json
{
  "agentCommand": ["opencode", "acp"]
}
```

### Authentication Setup

Run `/connect` in OpenCode's TUI to configure your provider:

- Select "opencode" for Zen, or choose another provider
- For Zen: sign in at [opencode.ai/auth](https://opencode.ai/auth), copy your API key
- For other providers: paste your provider's API key

### Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENCODE_API_KEY` | OpenCode Zen API key (access to all curated models) |
| `ANTHROPIC_API_KEY` | If using Claude directly |
| `OPENAI_API_KEY` | If using OpenAI directly |

OpenCode reads its own config file for LLM provider settings. See [opencode.ai/docs](https://opencode.ai/docs) for provider configuration.

### ACP Features Supported

- Built-in tools (file operations, terminal commands)
- Custom tools and slash commands
- MCP servers from OpenCode config
- Project-specific rules from `AGENTS.md`
- Custom formatters and linters
- Agents: `build` (full-access, default) and `plan` (read-only analysis)
- Permissions system
- Subagent support (`@general` for complex searches)

### Agents (Modes)

OpenCode ships with two built-in agents switchable via ACP:

| Agent | Description |
|-------|-------------|
| `build` | Default, full-access agent for development work |
| `plan` | Read-only agent for analysis and code exploration |

### Limitations

Some built-in slash commands like `/undo` and `/redo` are currently unsupported over ACP.

### Verify It Works

```bash
pnpm dev

# Open http://localhost:5173
# Create a task, send a message
# You should see OpenCode's streaming response
# Try switching to "plan" mode if the bridge exposes it
```

---

## Other Agents

Any ACP-compatible agent works. Here are more examples:

| Agent | Install | Command |
|-------|---------|---------|
| Gemini CLI | [gemini.google.com/cli](https://gemini.google.com/cli) | `["gemini", "cli", "acp"]` |
| Cursor | Built into Cursor IDE | `["cursor", "--acp"]` |
| Cline | [cline.bot](https://cline.bot) | `["cline", "--acp"]` |
| GitHub Copilot | Part of GitHub CLI (public preview) | `["github-copilot-cli", "--acp"]` |
| Goose | [github.com/block/goose](https://github.com/block/goose) | `["goose", "--acp"]` |
| Junie | JetBrains AI agent | `["junie", "--acp"]` |
| Qwen Code | [github.com/QwenLM/qwen-code](https://github.com/QwenLM/qwen-code) | `["qwen-code", "acp"]` |

See the full list of 33+ ACP agents at [agentclientprotocol.com/get-started/agents](https://agentclientprotocol.com/get-started/agents).

---

## Troubleshooting

### `FileNotFoundError: [WinError 2] The system cannot find the file specified` (Windows)

The agent binary is not on `PATH`. `asyncio.create_subprocess_exec` calls Windows `CreateProcess`, which only runs `.exe` files. Globally-installed npm shims ship as `.cmd` / `.ps1` files; the bridge auto-wraps those with `cmd.exe /c` in `backend/agent/runner.py:_resolve_windows_command` — **but only if `shutil.which` can resolve the binary**. If the binary isn't installed at all, you'll see WinError 2 instead.

**Fix one of three ways:**

1. Install the agent globally via npm:
   ```
   npm install -g @agentclientprotocol/claude-agent-acp
   ```
2. Use the `npx` form in your `agentCommand` (the dropdown in `ProjectSelector.tsx` already does this for Claude and Codex):
   ```json
   { "agentCommand": ["npx", "-y", "@agentclientprotocol/claude-agent-acp"] }
   ```
3. Run `pnpm install` at the repo root — npm-installable agents are declared as `devDependencies` so they're cached locally and `npx -y` resolves instantly.

For native binaries (Kiro, OpenCode, Gemini, etc.) you must install them separately — they're not on npm.
