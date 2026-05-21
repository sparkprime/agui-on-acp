# Custom Workspace UI Demo

> **Full-control frontend:** A complete workspace UI consuming AG-UI events directly via REST + raw SSE parsing. No framework abstraction — you own every pixel.

## What This Shows

This is a full-featured web workspace built from scratch on top of the ACP → AG-UI bridge. It demonstrates what you can build when you consume AG-UI events at the lowest level — direct `fetch()` calls to REST endpoints and manual SSE stream parsing.

### Features

- Streaming chat with markdown rendering
- Tool call visualization (ToolCards with live status)
- Human-in-the-loop approval dialogs with permission options
- Thinking/reasoning detection (retroactive based on tool call patterns)
- Session management (create, resume, delete, rename)
- Slash command autocomplete (from agent-advertised commands)
- File path @-mentions with autocomplete
- Image/file attachments
- Git integration (status, diff, branches)
- File browser
- Mode and model switching
- Context usage indicator
- MCP server status
- Multi-session support with sidebar

## How It Consumes AG-UI

Unlike the CopilotKit demo (framework handles everything) or HttpAgent demo (client library gives you typed events), this frontend:

1. **Creates sessions** via `POST /v2/tasks`
2. **Starts runs** via `POST /v2/tasks/{id}/run`
3. **Parses SSE manually** from `GET /v2/tasks/{id}/events?runId=...` using `fetch + ReadableStream`
4. **Handles approvals** via `POST /v2/tasks/{id}/approval`
5. **Dispatches events** to a Zustand store based on AG-UI event types

This gives complete control over transport, buffering, error recovery, and UI state — at the cost of more code.

## Architecture

```
src/
├── App.tsx                    # Main layout, session orchestration
├── components/
│   ├── ChatPanel.tsx          # Message input, rendering, streaming display
│   ├── ApprovalDialog.tsx     # Tool permission approval UI
│   ├── ToolCard.tsx           # Individual tool call visualization
│   ├── ThinkingBlock.tsx      # Reasoning/thinking indicator
│   ├── SessionSidebar.tsx     # Multi-session management
│   ├── TaskPanel.tsx          # Kanban-style task board
│   ├── ConfigPanel.tsx        # Mode/model configuration
│   └── ProjectSelector.tsx    # Working directory picker
├── hooks/
│   └── useAgUiStream.ts       # AG-UI SSE lifecycle hook
├── services/
│   ├── aguiClient.ts          # Raw SSE connection (fetch + ReadableStream)
│   ├── aguiTypes.ts           # AG-UI event type definitions
│   ├── v2Api.ts               # REST client for bridge endpoints
│   ├── api.ts                 # Side-channel APIs (files, git)
│   ├── slashCommands.ts       # Slash command definitions
│   └── taskStore.ts           # Task/TODO persistence
├── stores/
│   └── sessionStore.ts        # Zustand state (sessions, messages, tools)
└── types.ts                   # Shared TypeScript types
```

## Setup

```bash
# 1. Start the bridge (from project root)
cd ../..
pnpm dev:backend

# 2. Install and run this demo
cd example-frontends/custom-workspace-ui-demo
pnpm install
pnpm dev
# Open http://localhost:3000
```

Or use the root shortcut (starts both backend + this UI):

```bash
pnpm dev
```

## When to Use This

Use this approach when you need:
- Full control over the UI/UX
- Custom state management beyond what frameworks offer
- Deep integration with side-channel APIs (files, git)
- Custom streaming behavior (buffering, batching, retry logic)
- A starting point for a production workspace product

## Comparison

| | Custom Workspace (this) | CopilotKit | HttpAgent |
|---|---|---|---|
| AG-UI consumption | Direct REST + raw SSE | Framework handles it | Client library |
| Lines of code | ~2000 | ~20 | ~50 |
| UI control | Total | Theme + labels | Full (you build it) |
| Streaming | Manual `fetch` + `ReadableStream` | Built-in | Observable subscription |
| State management | Zustand (custom) | Built-in | Your choice |
| Approval flow | Custom dialog | Built-in | Your implementation |
| Side-channel APIs | Files, git, commands | Not included | Not included |

## Tech Stack

- React 19
- Vite
- Tailwind CSS
- Zustand (state management)
- xterm.js (terminal rendering)
- lucide-react (icons)
- react-markdown + react-syntax-highlighter
