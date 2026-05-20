# Why AG-UI: What Developers Get Once Events Are Flowing

This bridge translates ACP into AG-UI events. But why AG-UI specifically? What does it unlock for developers building on top of this?

---

## The Short Answer

Once your ACP agent's output is streaming as AG-UI events, you're plugged into an **entire ecosystem of frontend tools, SDKs, and frameworks** that already understand these events. You don't need to build everything from scratch.

---

## Option 1: CopilotKit (Plug-and-Play React UI)

[CopilotKit](https://docs.copilotkit.ai) is the primary AG-UI client. It provides React components that consume AG-UI events out of the box.

```tsx
import { CopilotKit } from "@copilotkit/react-core";
import { CopilotChat } from "@copilotkit/react-ui";

function App() {
  return (
    <CopilotKit runtimeUrl="/api/copilotkit">
      <CopilotChat />
    </CopilotKit>
  );
}
```

**What you get for free:**
- Streaming chat UI with markdown rendering
- Tool call visualization
- Human-in-the-loop approval dialogs
- Shared state synchronization between agent and UI
- Frontend tool calls (agent can request UI actions)
- Generative UI (agent can propose React components to render)

**The `useCoAgent` hook:**
```tsx
import { useCoAgent } from "@copilotkit/react-core";

const { state, setState, run, stop } = useCoAgent({
  name: "my_agent",
  initialState: { tasks: [], currentFile: null },
});

// state updates in real-time as the agent works
// setState lets you push changes back to the agent
```

This means you could point CopilotKit at our bridge's SSE endpoint and get a full-featured agent UI without writing any event handling code.

---

## Option 2: Raw AG-UI Client (Build Your Own UI)

The `@ag-ui/core` TypeScript SDK provides types and utilities for consuming events directly:

```typescript
import { HttpAgent } from "@ag-ui/client";

const agent = new HttpAgent({ url: "http://localhost:8000/v2/tasks/{id}/events" });

const stream = agent.run({
  threadId: "task-123",
  runId: "run-456",
  input: { messages: [{ role: "user", content: "Hello" }] },
});

stream.subscribe({
  next: (event) => {
    switch (event.type) {
      case "TEXT_MESSAGE_CONTENT":
        appendToChat(event.delta);
        break;
      case "TOOL_CALL_START":
        showToolCard(event.toolCallName);
        break;
      case "STATE_UPDATE":
        updateSidebar(event.state);
        break;
    }
  },
});
```

This is what our reference UI does — it's a custom React app that consumes AG-UI events via our `useAgUiStream` hook.

---

## Option 3: Use Any AG-UI-Compatible Client

AG-UI has SDKs in **9+ languages** and clients beyond just web:

| Client | Use Case |
|--------|----------|
| CopilotKit (React) | Full-featured web UI |
| React Native (community) | Mobile apps |
| Terminal + Agent (community) | CLI-based consumption |
| Custom WebSocket client | Real-time dashboards |
| Slack/Discord bot | Consume events in chat platforms |

Because AG-UI is transport-agnostic (SSE, WebSocket, webhooks), you can build clients in any environment.

---

## What AG-UI Event Types Enable

### Beyond Basic Chat

Most people think "agent UI = chat bubble." AG-UI enables much richer patterns:

| Pattern | AG-UI Events Used | What It Enables |
|---------|-------------------|-----------------|
| **Streaming chat** | TEXT_MESSAGE_START/CONTENT/END | Real-time typewriter-style text |
| **Tool visualization** | TOOL_CALL_START/ARGS/END | Show what the agent is doing (file edits, commands, API calls) |
| **Human-in-the-loop** | STATE_UPDATE (approval) + RunFinished (interrupt) | Pause agent, get user approval, resume |
| **Shared state** | STATE_SNAPSHOT + STATE_DELTA | Sync agent's internal state to UI (task lists, file trees, plans) |
| **Generative UI** | CUSTOM + tool calls | Agent proposes UI components to render |
| **Reasoning visibility** | CUSTOM (thinking events) | Show agent's thought process |
| **Multi-step progress** | STEP_STARTED/FINISHED | Progress bars, step indicators |
| **Activity tracking** | CUSTOM (activities) | Show what the agent is researching, planning, executing |

### Concrete Examples

**A Task Board UI:**
- Agent sends STATE_DELTA with JSON Patch updating task statuses
- Frontend renders a Kanban board that updates in real-time as the agent works

**A Code Review UI:**
- Agent sends TOOL_CALL events showing file reads and edits
- Frontend renders a diff viewer showing changes as they happen
- STATE_UPDATE triggers approval dialog for each file change

**A Deployment Dashboard:**
- Agent sends STEP_STARTED/FINISHED for each deployment phase
- Frontend renders a pipeline visualization
- Interrupts pause for manual approval at critical gates

---

## The Ecosystem Advantage

By emitting AG-UI events (instead of a custom protocol), you tap into:

### Frameworks That Already Speak AG-UI (Agent → Backend)
- LangGraph
- CrewAI
- Google ADK
- AWS Strands Agents
- Microsoft Agent Framework
- Pydantic AI
- Agno
- LlamaIndex
- AG2
- Mastra

### Infrastructure That Supports AG-UI
- Amazon Bedrock AgentCore (native AG-UI support)
- CopilotKit Runtime

### SDKs for Building Custom Integrations
- TypeScript/JavaScript (`@ag-ui/core`)
- Python (`ag-ui`)
- Kotlin, Go, Dart, Java, Rust, Ruby, C++ (community)

---

## How This Bridge Fits In

```
┌──────────────────────────────────────────────────────────────┐
│                     AG-UI Ecosystem                            │
│                                                              │
│  CopilotKit ──┐                                             │
│  Custom React ─┤                                             │
│  Mobile App ───┤── consume AG-UI events ──┐                  │
│  Dashboard ────┤                          │                  │
│  Slack Bot ────┘                          │                  │
│                                           ▼                  │
│                              ┌─────────────────────┐         │
│                              │  THIS BRIDGE         │         │
│                              │  (ACP → AG-UI)       │         │
│                              └──────────┬──────────┘         │
│                                         │                    │
│                              ┌──────────▼──────────┐         │
│                              │  ACP Ecosystem       │         │
│                              │  (33+ agents)        │         │
│                              └─────────────────────┘         │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

This bridge is the **connector** between two ecosystems:
- **Below:** 33+ ACP coding agents (all the CLI agents trapped in terminals)
- **Above:** The entire AG-UI frontend ecosystem (CopilotKit, custom UIs, mobile, dashboards)

Without this bridge, ACP agents can't participate in the AG-UI world. With it, any ACP agent instantly becomes compatible with every AG-UI frontend.

---

## Getting Started: Three Paths

### Path 1: Use the Reference UI (fastest)
```bash
pnpm dev  # Bridge + reference UI at localhost:5173
```
You get a working chat UI immediately. Customize the React components to your needs.

### Path 2: Point CopilotKit at the Bridge
```tsx
<CopilotKit runtimeUrl="http://localhost:8000">
  <CopilotChat />
</CopilotKit>
```
Full-featured UI with shared state, generative UI, and frontend tool calls — powered by your ACP agent.

### Path 3: Build a Custom Client
Consume the SSE stream directly from any language/framework:
```
GET http://localhost:8000/v2/tasks/{id}/events?runId={runId}
Accept: text/event-stream
```
Parse AG-UI events and render however you want — terminal, mobile, Electron, web component, Discord bot.

---

## Summary

| What You Get | How |
|-------------|-----|
| Streaming chat | TEXT_MESSAGE events → any text renderer |
| Tool visualization | TOOL_CALL events → custom tool cards |
| Approval flows | STATE_UPDATE + REST → dialog + confirm |
| Real-time state sync | STATE_SNAPSHOT/DELTA → reactive UI |
| Agent reasoning | CUSTOM events → thinking indicators |
| Progress tracking | STEP events → progress bars |
| Portable frontend | AG-UI is framework-agnostic |
| Ecosystem access | CopilotKit, SDKs in 9+ languages |
| Agent swapping | Change one config line, keep your UI |
