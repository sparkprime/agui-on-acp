# CopilotKit + ACP Agent Demo

> **The "blow them away" demo:** A full-featured AI chat UI on top of your custom ACP agent in ~30 lines of React.

## What This Shows

You built a custom coding agent (or you're using kiro-cli, claude-agent-acp, Codex, etc.). It speaks ACP over stdio. Normally it lives in a terminal.

With our bridge + CopilotKit, you get a **production-quality chat UI** with:

- Streaming markdown messages
- Tool call visualization
- Human-in-the-loop approval dialogs
- Shared state between agent and UI
- Generative UI capabilities
- Message history

**Total frontend code: ~30 lines.**

## How It Works

```
┌─────────────────────────────┐
│  CopilotKit React UI        │  ← ~30 lines of your code
│  (streaming chat, tools,    │
│   approvals, state sync)    │
└──────────────┬──────────────┘
               │ AG-UI events (standard protocol)
               ▼
┌─────────────────────────────┐
│  ACP → AG-UI Bridge         │  ← our open-source project
│  (localhost:8000)            │
└──────────────┬──────────────┘
               │ JSON-RPC 2.0 (stdio)
               ▼
┌─────────────────────────────┐
│  Your ACP Agent              │  ← any of 33+ agents
│  (kiro-cli / claude / etc.) │
└─────────────────────────────┘
```

## Setup

### 1. Start the bridge (with your agent)

```bash
# In the project root
cd ../..
pnpm dev:backend
# Bridge running at localhost:8000, connected to your agent
```

### 2. Install and run this demo

```bash
cd examples/copilotkit-demo
pnpm install
pnpm dev
# Open http://localhost:3001
```

### 3. That's it

Type a message. Watch your ACP agent respond through CopilotKit's polished UI.

## The Code (entire App.tsx)

```tsx
import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";
import "@copilotkit/react-ui/styles.css";

export default function App() {
  return (
    <CopilotKit runtimeUrl="http://localhost:8000/api/copilotkit">
      <CopilotChat
        labels={{
          title: "ACP Agent",
          initial: "Connected to your ACP agent. Ask anything!",
        }}
      />
    </CopilotKit>
  );
}
```

That's the entire frontend. CopilotKit handles:
- SSE stream consumption (AG-UI events)
- Message rendering (markdown, code blocks)
- Tool call display
- Approval flow UI
- Loading states
- Error handling

## Why This Matters

**Before this bridge existed:**
- Custom ACP agent → terminal only
- Want a web UI? Build everything: SSE parsing, state management, approval flows, streaming renderer

**After:**
- Custom ACP agent → bridge → CopilotKit
- Full production UI in 30 lines
- Swap agents by changing one config line
- Get every CopilotKit feature (generative UI, shared state, actions) for free

## Note on Runtime URL

The `runtimeUrl` points to our bridge. CopilotKit needs a runtime endpoint that speaks AG-UI. Our bridge's SSE endpoint at `/v2/tasks/{id}/events` emits standard AG-UI events.

For full CopilotKit integration, you'd add a thin `/api/copilotkit` endpoint to our bridge that implements CopilotKit's expected runtime handshake. The events themselves are already compatible — it's just routing.

Alternatively, use CopilotKit's `HttpAgent` directly:

```tsx
import { HttpAgent } from "@ag-ui/client";

const agent = new HttpAgent({
  url: "http://localhost:8000/v2/tasks/{taskId}/events",
});
```

## Comparison: Reference UI vs CopilotKit

| Aspect | Reference UI (included) | CopilotKit |
|--------|------------------------|------------|
| Lines of code | ~2000 | ~30 |
| Setup time | 0 (included) | 5 min (npm install) |
| Customization | Full control | Theme + labels |
| Streaming | Custom SSE hook | Built-in |
| Tools | Custom ToolCard | Built-in |
| Approvals | Custom dialog | Built-in |
| State sync | Custom Zustand | useCoAgent hook |
| Generative UI | Not included | Built-in |
| Mobile | Not included | React Native SDK |

**Use the reference UI** when you want full control over every pixel.
**Use CopilotKit** when you want to ship fast and get production features for free.
