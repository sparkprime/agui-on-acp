# Integration Contract

The complete REST + SSE API specification for building custom frontends on top of the ACP → AG-UI Bridge. You should not need to read backend source code to integrate.

**Base URL:** `http://localhost:8000` (configurable via `bridge.config.json`)

---

## Task Lifecycle

The core interaction: create a task, start a run with a prompt, stream AG-UI events, handle approvals.

### POST /v2/tasks — Create Task

Spawns an ACP agent subprocess and initializes the protocol.

**Request:**
```json
{
  "cwd": "/path/to/project",
  "title": "My Task",
  "resumeSessionId": null,
  "mode": "code",
  "model": null,
  "mcpServers": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `cwd` | string | Yes | Workspace directory path |
| `title` | string | No | Task title (default: "New Task") |
| `resumeSessionId` | string | No | Session ID to resume |
| `mode` | string | No | Initial agent mode |
| `model` | string | No | Model identifier |
| `mcpServers` | object | No | MCP server configuration |

**Response (200):**
```json
{
  "taskId": "task_abc123",
  "agentSessionId": "session_xyz",
  "runUrl": "/v2/tasks/task_abc123/run",
  "eventsUrl": "/v2/tasks/task_abc123/events",
  "modes": [{ "id": "code", "name": "Code" }],
  "models": [{ "id": "claude-sonnet-4", "name": "Sonnet" }],
  "currentModeId": "code"
}
```

---

### POST /v2/tasks/{taskId}/run — Start Run

Send a user prompt and begin streaming AG-UI events.

**Request:**
```json
{
  "input": {
    "messages": [
      {
        "role": "user",
        "content": "Help me refactor this function",
        "attachments": []
      }
    ]
  },
  "config": null
}
```

**Response (200):**
```json
{
  "runId": "run_def456"
}
```

Use the returned `runId` to connect to the SSE event stream.

---

### GET /v2/tasks/{taskId}/events?runId={runId} — Stream Events

**Server-Sent Events stream.** Connect immediately after starting a run.

**Headers:**
```
Accept: text/event-stream
```

**Response:** `text/event-stream` with AG-UI events. Stream closes after `RUN_FINISHED` or `RUN_ERROR`.

See [Event Types](#event-types) below for the full event schema.

---

### POST /v2/tasks/{taskId}/approval — Resolve Approval

When a `STATE_UPDATE` event indicates a pending approval, resolve it here.

**Request:**
```json
{
  "callId": "tool_call_xyz",
  "approved": true,
  "optionId": "allow_once"
}
```

**Response (200):**
```json
{
  "success": true,
  "callId": "tool_call_xyz"
}
```

---

### POST /v2/tasks/{taskId}/cancel — Cancel Run

Cancel the agent's current turn.

**Response (200):**
```json
{
  "success": true,
  "taskId": "task_abc123"
}
```

---

### POST /v2/tasks/{taskId}/stop — Stop Agent Process

Kill the agent subprocess but keep task metadata for potential revival.

---

### DELETE /v2/tasks/{taskId} — Delete Task

Kill agent and remove from store.

---

### GET /v2/tasks — List Tasks

**Response:**
```json
{
  "tasks": [
    {
      "taskId": "task_abc123",
      "agentSessionId": "session_xyz",
      "cwd": "/path/to/project",
      "title": "My Task",
      "status": "idle",
      "createdAt": "2025-01-15T10:30:00Z",
      "updatedAt": "2025-01-15T10:35:00Z"
    }
  ]
}
```

---

### POST /v2/tasks/{taskId}/mode — Switch Mode

```json
{ "modeId": "architect" }
```

### POST /v2/tasks/{taskId}/model — Switch Model

```json
{ "modelId": "claude-sonnet-4" }
```

### POST /v2/tasks/{taskId}/command — Execute Command

```json
{ "command": "/web", "args": { "args": "search query" } }
```

---

## Event Types

All events are JSON objects with a `type` field. Streamed as SSE with `event: {TYPE}` and `data: {json}`.

### RUN_STARTED
```json
{
  "type": "RUN_STARTED",
  "runId": "run_def456",
  "taskId": "task_abc123",
  "threadId": "task_abc123",
  "timestamp": 1707820800.0
}
```

### TEXT_MESSAGE_START
```json
{
  "type": "TEXT_MESSAGE_START",
  "messageId": "msg_001",
  "role": "assistant",
  "timestamp": 1707820801.0
}
```

### TEXT_MESSAGE_CONTENT
```json
{
  "type": "TEXT_MESSAGE_CONTENT",
  "messageId": "msg_001",
  "delta": "Here's what I found...",
  "timestamp": 1707820801.5
}
```

### TEXT_MESSAGE_END
```json
{
  "type": "TEXT_MESSAGE_END",
  "messageId": "msg_001",
  "timestamp": 1707820805.0
}
```

### TOOL_CALL_START
```json
{
  "type": "TOOL_CALL_START",
  "toolCallId": "tc_xyz",
  "toolCallName": "read_file",
  "parentMessageId": "msg_001",
  "timestamp": 1707820806.0
}
```

### TOOL_CALL_ARGS
```json
{
  "type": "TOOL_CALL_ARGS",
  "toolCallId": "tc_xyz",
  "delta": "{\"path\": \"src/main.ts\"}",
  "timestamp": 1707820806.1
}
```

### TOOL_CALL_END
```json
{
  "type": "TOOL_CALL_END",
  "toolCallId": "tc_xyz",
  "result": "file contents...",
  "timestamp": 1707820807.0
}
```

### STATE_UPDATE (Approval)
```json
{
  "type": "STATE_UPDATE",
  "state": {
    "approval": {
      "pending": true,
      "callId": "tc_xyz",
      "toolName": "write_file",
      "summary": "Permission required: write_file",
      "options": [
        { "optionId": "allow_once", "name": "Allow once", "kind": "allow_once" },
        { "optionId": "allow_always", "name": "Always allow", "kind": "allow_always" }
      ],
      "category": "filesystem"
    }
  },
  "timestamp": 1707820808.0
}
```

### RUN_FINISHED
```json
{
  "type": "RUN_FINISHED",
  "runId": "run_def456",
  "taskId": "task_abc123",
  "timestamp": 1707820810.0
}
```

### RUN_ERROR
```json
{
  "type": "RUN_ERROR",
  "runId": "run_def456",
  "taskId": "task_abc123",
  "message": "Agent process exited unexpectedly",
  "code": null,
  "timestamp": 1707820810.0
}
```

### CUSTOM
```json
{
  "type": "CUSTOM",
  "name": "agent:mcp_initialized",
  "value": { "serverId": "browser-tools" },
  "timestamp": 1707820802.0
}
```

---

## Side-Channel APIs

### Files API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/files?path=.&base=/project` | List directory |
| GET | `/api/files/content?path=src/main.ts&base=/project` | Read file |
| POST | `/api/files` | Create file |
| PUT | `/api/files` | Update file |
| DELETE | `/api/files?path=old.txt&base=/project` | Delete file |
| POST | `/api/files/mkdir?path=new-dir&base=/project` | Create directory |

### Git API

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/git/status?dir=/project` | Repository status |
| GET | `/api/git/log?dir=/project&limit=50` | Commit log |
| GET | `/api/git/diff?dir=/project&staged=false` | Diff |
| GET | `/api/git/branches?dir=/project` | List branches |
| POST | `/api/git/commit` | Create commit |
| POST | `/api/git/stage?dir=/project&file=src/main.ts` | Stage file |
| POST | `/api/git/unstage?dir=/project&file=src/main.ts` | Unstage file |

---

## Health Check

```
GET /health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "project": "acp-to-agui"
}
```
