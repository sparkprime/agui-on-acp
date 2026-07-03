# Roadmap

## Planned: Claude Code Subagent → ACP ConfigOption Mapping

### The Gap

Kiro and Claude Code both support custom agents, but they expose them over ACP differently:

| Capability | Kiro CLI | Claude Code (via `claude-code-acp`) |
|---|---|---|
| Custom agent definitions | `.kiro/agents/*.json` | `.claude/agents/*.md` |
| Exposed over ACP as modes/configOptions | Yes | **No** |
| ACP modes represent | Agent personality + tools + prompt | Permission level only |
| `session/set_mode` switches | Between custom agents | Between permission modes (default, acceptEdits, auto, plan, dontAsk, bypassPermissions) |
| `session/set_config_option` categories | mode + model | mode + model + effort |

When you connect Kiro via ACP, your bridge receives 13+ modes — including custom agents you've defined. When you connect Claude Code via ACP, you get only 5-6 permission modes regardless of how many `.claude/agents/` subagents exist.

This is because `claude-code-acp` (the Zed-maintained adapter at `github.com/zed-industries/claude-code-acp`) has **zero code** that discovers or surfaces subagent definitions. Its `buildAvailableModes()` function returns a hardcoded set of permission modes, and `buildConfigOptions()` only exposes mode/model/effort selectors.

### What Claude Code Subagents Are

Claude Code subagents are markdown files with YAML frontmatter stored in:
- `.claude/agents/` (project-level)
- `~/.claude/agents/` (user-level)

Each defines a specialized AI assistant with:

```yaml
---
name: code-reviewer
description: Reviews code for quality and best practices
tools: Read, Grep, Glob, Bash
model: sonnet
permissionMode: default
---

You are a senior code reviewer. Focus on quality, security, and best practices.
```

Key fields: `name`, `description`, `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `mcpServers`, `hooks`, `skills`, `memory`, `effort`.

Inside Claude Code, these are used for internal task delegation. But there's no reason they can't be exposed as selectable agent configurations over ACP — the same way Kiro does it.

### What We Want To Build

A wrapper layer in this bridge that:

1. **Discovers** `.claude/agents/*.md` files from the configured working directory
2. **Parses** the YAML frontmatter to extract agent metadata
3. **Maps** each subagent to an ACP `ConfigOption` with `category: "mode"` (or a custom `category: "agent"`)
4. **Exposes** them in the `configOptions` array returned during session setup
5. **Handles** `session/set_config_option` to switch the active agent mid-session
6. **Applies** the selected agent's constraints (tools, model, permissionMode, prompt) to the running session

### Protocol Design

The ACP spec recently stabilized `session/set_config_option` as the preferred replacement for `session/set_mode`. Config options support arbitrary categories and are the right abstraction for this.

**Proposed configOption entry for a subagent:**

```json
{
  "id": "agent",
  "name": "Agent",
  "description": "Select the active agent personality",
  "category": "agent",
  "type": "select",
  "currentValue": "default",
  "options": [
    { "value": "default", "name": "Default", "description": "Standard Claude Code agent" },
    { "value": "code-reviewer", "name": "Code Reviewer", "description": "Reviews code for quality and best practices" },
    { "value": "debugger", "name": "Debugger", "description": "Debugging specialist for errors and test failures" }
  ]
}
```

**On selection change**, the bridge would:
1. Receive `session/set_config_option { configId: "agent", value: "code-reviewer" }`
2. Load the full agent definition from disk
3. Apply tool restrictions, model override, and permission mode to the session
4. Inject the agent's system prompt as context for subsequent prompts
5. Return the updated full `configOptions` array (since model/effort may change based on agent definition)

### Architecture

```
┌─────────────────────────────────────────────────┐
│  Client (CopilotKit / HttpAgent / etc.)          │
│  Shows agent picker dropdown from configOptions │
└───────────────────────┬─────────────────────────┘
                        │ POST /ag-ui or REST API
                        ▼
┌─────────────────────────────────────────────────┐
│  This Bridge (Python / FastAPI)                 │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │  NEW: AgentDiscovery                      │  │
│  │  - Scans .claude/agents/*.md              │  │
│  │  - Parses YAML frontmatter               │  │
│  │  - Builds configOption for agent picker   │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │  NEW: AgentSwitcher                       │  │
│  │  - Handles set_config_option(agent, ...)  │  │
│  │  - Applies tools/model/prompt/permissions │  │
│  │  - Coordinates with AcpProtocol layer     │  │
│  └───────────────────────────────────────────┘  │
│                                                 │
│  ┌───────────────────────────────────────────┐  │
│  │  Existing: AcpToAguiBridge                │  │
│  │  - Translates ACP events → AG-UI events   │  │
│  │  - Now also emits CONFIG_OPTION_UPDATE    │  │
│  └───────────────────────────────────────────┘  │
└───────────────────────┬─────────────────────────┘
                        │ JSON-RPC 2.0 / stdio
                        ▼
┌─────────────────────────────────────────────────┐
│  claude-code-acp (unchanged)                    │
│  Still exposes permission modes only            │
│  We work around it at the bridge layer          │
└─────────────────────────────────────────────────┘
```

### Open Questions

1. **Session-level vs prompt-level?** Should switching agents restart the ACP session (clean slate) or inject the agent's system prompt into the next `session/prompt` call? Kiro likely restarts; the lightweight approach is prompt injection.

2. **Tool restriction enforcement.** Claude Code's `tools` field in subagent definitions restricts what the subagent can use. But over ACP, tool calls come from the agent subprocess — we'd need to either pass tool restrictions to `claude-code-acp` (if it supports it) or enforce them at the bridge by rejecting disallowed tool calls.

3. **Model override.** If a subagent specifies `model: haiku`, we'd need to call `session/set_config_option` on the underlying ACP session to switch models. This should work since `claude-code-acp` already exposes model as a config option.

4. **Custom category or reuse "mode"?** The ACP spec says categories starting with `_` are reserved for custom use. We could use `category: "_agent"` to distinguish from permission modes, or use a non-underscore category like `"agent"` since the spec says categories are purely UX hints.

5. **Upstream contribution.** Long-term, this logic belongs in `claude-code-acp` itself. We could contribute subagent discovery upstream once the pattern is proven in this bridge.

### Priority

Medium — the bridge works today with both Kiro and Claude Code. This enhancement would make Claude Code's subagent ecosystem accessible from web UIs, bringing parity with Kiro's agent-switching UX.

### Dependencies

- ACP `session/set_config_option` (stabilized, available now)
- Python YAML frontmatter parser (e.g., `python-frontmatter`)
- File watching for hot-reload of agent definitions (optional, nice-to-have)
