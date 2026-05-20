# AG-UI HttpAgent Demo

> Use `@ag-ui/client`'s HttpAgent directly — no CopilotKit, just the raw AG-UI protocol.

## What This Shows

The simplest possible frontend consuming AG-UI events from our bridge. Uses:
- `@ag-ui/client` → `HttpAgent` (POSTs to `/ag-ui`, subscribes to SSE events)
- React for rendering
- `react-markdown` for message formatting

This is the "full control" approach — you handle every event yourself.

## Setup

```bash
# 1. Start the bridge (from project root)
cd ../..
PYTHONPATH=. uvicorn backend.main:app --port 8000 &

# 2. Install and run this demo
cd examples/httpagent-demo
pnpm install
pnpm dev
# Open http://localhost:3002
```

## The Key Code

```typescript
import { HttpAgent } from "@ag-ui/client";
import { EventType } from "@ag-ui/core";

const agent = new HttpAgent({ url: "http://localhost:8000/ag-ui" });

const runAgent = agent.run({
  threadId: "my-thread",
  runId: "my-run",
  messages: [{ id: "1", role: "user", content: "Hello!" }],
  tools: [],
  state: {},
});

const observable = runAgent();
observable.subscribe({
  next: (event) => {
    if (event.type === EventType.TEXT_MESSAGE_CONTENT) {
      console.log(event.delta); // streaming text chunks
    }
  },
});
```

## Why This Matters

This proves the bridge is a **standard AG-UI server** — not just compatible with our reference UI or CopilotKit, but with the raw `@ag-ui/client` library too. Any developer can build any frontend using the AG-UI SDK directly.
