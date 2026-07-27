# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                     Python Backend (FastAPI)                             │
│                        localhost:9001                                    │
│                                                                         │
│  ┌────────────────────────────────────────────────────────────────────┐ │
│  │                    AG-UI Endpoint                                   │ │
│  │  POST /ag-ui              — Standard AG-UI run (fresh + resume)     │ │
│  │  GET  /health             — Liveness probe                          │ │
│  └───────────────────────────┬────────────────────────────────────────┘ │
│                              │                                           │
│  ┌───────────────────────────┼───────────────────────────────────────┐  │
│  │                     SessionManager                                 │  │
│  │  ┌──────────────────────┐  ┌──────────────────────────────────┐   │  │
│  │  │ AgentRunner          │  │ AcpToAguiBridge                  │   │  │
│  │  │ (ACP proc)           │  │ (event translator)               │   │  │
│  │  └──────┬───────────────┘  └──────────────────────────────────┘   │  │
│  │         │                                                          │  │
│  │               ┌──────────┴──────────┐                             │  │
│  │               │ AcpProtocol         │                             │  │
│  │               │ (JSON-RPC interface) │                             │  │
│  │               └──────────┬──────────┘                             │  │
│  └──────────────────────────┼────────────────────────────────────────┘  │
└──────────────────────────────┼──────────────────────────────────────────┘
                               │
                               │ stdin/stdout (JSON-RPC 2.0 / ndjson)
                               ▼
                    ┌──────────────────────┐
                    │   ACP Agent          │
                    │   (subprocess)       │
                    └──────────────────────┘
```

All session state is held in memory by `SessionManager` — there is no
database. Restarting the process loses active sessions; AG-UI clients
should start a fresh `threadId` after a bridge restart.

## Core Components

### Backend

| Module | Path | Description |
|--------|------|-------------|
| Main | `agui_on_acp/main.py` | FastAPI app, lifespan, router setup |
| Config | `agui_on_acp/config.py` | Bridge config loader (env var / `bridge.config.json`) |
| Agent Runner | `agui_on_acp/agent/runner.py` | Subprocess management (parameterized command) |
| ACP Protocol | `agui_on_acp/agent/acp_protocol.py` | Typed JSON-RPC interface |
| Bridge | `agui_on_acp/bridge/acp_to_agui.py` | ACP notification → AG-UI event translator (interrupt/resume HITL) |
| AG-UI Events | `agui_on_acp/agui/events.py` | Pydantic event type models (incl. Interrupt, InterruptOutcome) |
| SSE Encoder | `agui_on_acp/agui/sse.py` | SSE stream encoding (cancel-on-disconnect) |
| Session Manager | `agui_on_acp/sessions/manager.py` | In-memory session lifecycle (start_run, resume_run, cancel_run) |
| AG-UI Endpoint | `agui_on_acp/agui_endpoint.py` | Standard POST /ag-ui (fresh + resume routing) |

## Data Flow

### 1. Client Sends Message

```
AG-UI client → POST /ag-ui (RunAgentInput)
  → SessionManager.start_run()
  → AcpProtocol.prompt() → AgentRunner → agent subprocess (stdin)
```

### 2. Agent Response Stream

```
agent (stdout) → AgentRunner._read_stdout()
  → AcpToAguiBridge.session_update()
  → AG-UI events → asyncio.Queue
  → SSE stream (POST /ag-ui response)
  → AG-UI client renders
```

### 3. Tool Execution with Approval (Interrupt/Resume)

```
Agent requests tool → ACP request_permission() callback
  → AcpToAguiBridge._suspend_run()
  → RUN_FINISHED{outcome:interrupt, interrupts:[{id=callId, toolCallId=callId}]}
  → prompt task parks at await Future
  → SSE stream 1 closes

Client resumes → POST /ag-ui {resume:[{interruptId=callId, status:"resolved", payload}]}
  → SessionManager.resume_run()
  → bridge.attach_resume_queue() → RUN_STARTED
  → bridge.resolve_permission() → Future resolved
  → prompt task wakes, continues emitting
  → SSE stream 2: continuation events...
  → RUN_FINISHED (no outcome) — stream 2 closes
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
| `TOOL_CALL_RESULT` | Server → Client | Tool output (synthesizes ToolMessage) |
| `STATE_UPDATE` | Server → Client | State change (non-approval) |
| `STATE_SNAPSHOT` | Server → Client | Modes/models advertisement |
| `RUN_FINISHED` | Server → Client | Run completed (may carry interrupt outcome) |
| `RUN_ERROR` | Server → Client | Run failed |
| `CUSTOM` | Server → Client | Extension events |

## Session Lifecycle

```
Create → Initialize ACP → Idle ←→ Running → Idle
                                     ↓
                            Interrupted (awaiting resume) → Resumed → Running
                                     ↓
                            Cancelled (Futures resolved as cancelled) → Idle
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
