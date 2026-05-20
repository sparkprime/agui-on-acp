# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              Browser                                     │
│  ┌─────────────────────────────────────────────────────────────────┐   │
│  │                     React Frontend (Vite)                        │   │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │   │
│  │  │ Chat     │  │ Approval │  │ Tool     │  │ Session      │   │   │
│  │  │ Panel    │  │ Dialog   │  │ Cards    │  │ Sidebar      │   │   │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────────┘   │   │
│  │       │             │             │              │             │   │
│  │       │ SSE Stream  └─────────────┴──────────────┘             │   │
│  │       │                           │ REST                       │   │
│  └───────┼───────────────────────────┼───────────────────────────┘   │
└──────────┼───────────────────────────┼──────────────────────────────────┘
           │                           │
           ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     Python Backend (FastAPI)                             │
│                        localhost:8000                                    │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                    Task Routes (AG-UI)                              │ │
│  │  POST /v2/tasks          — Create task (spawn agent)              │ │
│  │  POST /v2/tasks/{id}/run — Start run (send prompt)                │ │
│  │  GET  /v2/tasks/{id}/events — SSE event stream                    │ │
│  │  POST /v2/tasks/{id}/approval — Approve/reject tool               │ │
│  └───────────────────────────┬────────────────────────────────────────┘ │
│                              │                                           │
│  ┌───────────────────────────┼───────────────────────────────────────┐  │
│  │                     TaskManager                                    │  │
│  │  ┌─────────────┐  ┌──────┴──────┐  ┌──────────────────────────┐  │  │
│  │  │ TaskStore   │  │ AgentRunner │  │ AcpToAguiBridge          │  │  │
│  │  │ (SQLite)    │  │ (ACP proc)  │  │ (event translator)       │  │  │
│  │  └─────────────┘  └──────┬──────┘  └──────────────────────────┘  │  │
│  │                          │                                        │  │
│  │               ┌──────────┴──────────┐                             │  │
│  │               │ AcpProtocol         │                             │  │
│  │               │ (JSON-RPC interface) │                             │  │
│  │               └──────────┬──────────┘                             │  │
│  └──────────────────────────┼────────────────────────────────────────┘  │
│                             │                                            │
│  ┌──────────────────────────┴───────────────────────────────────────┐   │
│  │ Side-Channel APIs: /api/files │ /api/git                          │   │
│  └───────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────┼──────────────────────────────────────────┘
                               │
                               │ stdin/stdout (JSON-RPC 2.0 / ndjson)
                               ▼
                    ┌──────────────────────┐
                    │   ACP Agent          │
                    │   (subprocess)       │
                    └──────────────────────┘
```

## Core Components

### Backend

| Module | Path | Description |
|--------|------|-------------|
| Main | `backend/main.py` | FastAPI app, lifespan, router setup |
| Config | `backend/config.py` | Bridge config loader (`bridge.config.json`) |
| Agent Runner | `backend/agent/runner.py` | Subprocess management (parameterized command) |
| ACP Protocol | `backend/agent/acp_protocol.py` | Typed JSON-RPC interface |
| Bridge | `backend/bridge/acp_to_agui.py` | ACP notification → AG-UI event translator |
| AG-UI Events | `backend/agui/events.py` | Pydantic event type models |
| SSE Encoder | `backend/agui/sse.py` | SSE stream encoding |
| Task Manager | `backend/tasks/manager.py` | Task lifecycle orchestration |
| Task Store | `backend/tasks/store.py` | SQLite-backed persistence |
| Task Routes | `backend/tasks/routes.py` | REST API endpoints |
| Policy Engine | `backend/policy/tool_policy.py` | Tool approval rules |
| Files API | `backend/api/files.py` | File system operations |
| Git API | `backend/api/git.py` | Git operation endpoints |

### Frontend

| Module | Path | Description |
|--------|------|-------------|
| App | `App.tsx` | Main application layout |
| Chat Panel | `components/ChatPanel.tsx` | AI chat with message history |
| Approval Dialog | `components/ApprovalDialog.tsx` | Tool approval modal |
| Tool Card | `components/ToolCard.tsx` | Tool execution display |
| Session Sidebar | `components/SessionSidebar.tsx` | Task list |
| AG-UI Stream | `src/hooks/useAgUiStream.ts` | SSE connection + event dispatch |
| AG-UI Client | `src/services/aguiClient.ts` | SSE stream consumer |
| Session Store | `stores/sessionStore.ts` | Zustand state (per-task) |

## Data Flow

### 1. User Sends Message

```
ChatPanel → useAgUiStream.startRun()
  → POST /v2/tasks/{taskId}/run (REST)
  → TaskManager.start_run()
  → AcpProtocol.session_prompt()
  → AgentRunner → agent subprocess (stdin)
```

### 2. Agent Response Stream

```
agent (stdout) → AgentRunner._read_stdout()
  → AcpToAguiBridge.handle_notification()
  → AG-UI events → asyncio.Queue
  → SSE stream (GET /v2/tasks/{taskId}/events)
  → useAgUiStream → Zustand store
  → ChatPanel re-renders
```

### 3. Tool Execution with Approval

```
Agent requests tool → ACP notification → Bridge
  → TOOL_CALL_START + TOOL_CALL_ARGS events
  → PolicyEngine.evaluate() → requires approval?
    Yes → STATE_UPDATE (pending approval)
        → ApprovalDialog renders
        → User approves → POST /v2/tasks/{taskId}/approval
        → TaskManager.approve() → AcpProtocol.respond()
        → Agent executes tool
    No  → Tool executes automatically
  → TOOL_CALL_END event
```

## AG-UI Event Types

| Event | Direction | Description |
|-------|-----------|-------------|
| `RUN_STARTED` | Server → Client | A new run has started |
| `TEXT_MESSAGE_START` | Server → Client | Beginning of assistant message |
| `TEXT_MESSAGE_CONTENT` | Server → Client | Incremental text delta |
| `TEXT_MESSAGE_END` | Server → Client | End of assistant message |
| `TOOL_CALL_START` | Server → Client | Tool invocation begins |
| `TOOL_CALL_ARGS` | Server → Client | Tool arguments (streaming) |
| `TOOL_CALL_END` | Server → Client | Tool execution completed |
| `STATE_UPDATE` | Server → Client | State change (approval, metadata) |
| `RUN_FINISHED` | Server → Client | Run completed |
| `RUN_ERROR` | Server → Client | Run failed |
| `CUSTOM` | Server → Client | Extension events |

## Task Lifecycle

```
Create → Initialize ACP → Idle ←→ Running → Idle
                                    ↓
                           Awaiting Approval → Approved/Rejected → Running
```

## ACP Protocol (Reference)

| Method | Direction | Description |
|--------|-----------|-------------|
| `initialize` | Bridge → Agent | Negotiate protocol version |
| `session/new` | Bridge → Agent | Create new session |
| `session/load` | Bridge → Agent | Resume existing session |
| `session/prompt` | Bridge → Agent | Send user message |
| `session/cancel` | Bridge → Agent | Cancel current turn |
| `session/set_mode` | Bridge → Agent | Switch agent mode |
| `session/update` | Agent → Bridge | Streaming updates |
| `session/request_permission` | Agent → Bridge | Request tool approval |

Full ACP spec: [agentclientprotocol.com](https://agentclientprotocol.com)
