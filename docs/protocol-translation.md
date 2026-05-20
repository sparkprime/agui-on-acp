# Protocol Translation: ACP → AG-UI

This document details exactly how ACP (Agent Client Protocol) notifications get translated into AG-UI events by the `AcpToAguiBridge`.

## Overview

ACP and AG-UI serve different purposes:
- **ACP** is a bidirectional JSON-RPC protocol between an agent process and its host (editor/IDE/bridge)
- **AG-UI** is a unidirectional event stream from a backend to a frontend (SSE)

The bridge sits between them, maintaining state and emitting properly sequenced AG-UI events.

## Translation Table

| ACP Input | Condition | AG-UI Output | Notes |
|-----------|-----------|--------------|-------|
| `session/update` → `agent_message_chunk` | First text in turn | `TEXT_MESSAGE_START` + `TEXT_MESSAGE_CONTENT` | Opens new message |
| `session/update` → `agent_message_chunk` | Subsequent text | `TEXT_MESSAGE_CONTENT` only | Same message ID |
| `session/update` → `tool_call` | No approval needed | `TOOL_CALL_START` + `TOOL_CALL_ARGS` | Closes open text message first |
| `session/update` → `tool_call` | Approval needed | + `STATE_UPDATE` (approval pending) | Frontend shows approval dialog |
| `session/update` → `tool_call_update` | `status=running` | `TOOL_CALL_ARGS` (progress) | Intermediate update |
| `session/update` → `tool_call_update` | `status=completed/failed` | `TOOL_CALL_END` | Removes from open set |
| `session/update` → `turn_end` | — | `TEXT_MESSAGE_END` + all `TOOL_CALL_END` + `RUN_FINISHED` | Closes everything |
| `session/update` → `current_mode_update` | — | `CUSTOM` (name: `agent:mode_update`) | Mode change |
| `session/request_permission` | — | `STATE_UPDATE` (approval pending) | RPC ID stored for response |
| Vendor extensions (`_*.dev/*`) | — | `CUSTOM` events | Normalized namespace |

## State Machine

The bridge maintains per-run state:

```
                    start_run()
                        │
                        ▼
              ┌─────────────────┐
              │  No Open State  │
              └────────┬────────┘
                       │
          agent_message_chunk
                       │
                       ▼
              ┌─────────────────┐
              │  Message Open   │──── agent_message_chunk ──→ (emit CONTENT)
              └────────┬────────┘
                       │
                  tool_call
                       │
          (close message: emit END)
                       │
                       ▼
              ┌─────────────────┐
              │  Tool Call Open │──── tool_call_update ──→ (emit ARGS/END)
              └────────┬────────┘
                       │
                  turn_end
                       │
          (close all: emit FINISHED)
                       │
                       ▼
              ┌─────────────────┐
              │    Run Done     │
              └─────────────────┘
```

## Sequencing Rules

1. **Only one text message can be open at a time.** If a `tool_call` arrives while a message is open, the bridge emits `TEXT_MESSAGE_END` first.

2. **Multiple tool calls can be open simultaneously.** Each gets its own `TOOL_CALL_START` and is tracked by ID until `TOOL_CALL_END`.

3. **`turn_end` closes everything.** Any open message gets `TEXT_MESSAGE_END`, all open tool calls get `TOOL_CALL_END`, then `RUN_FINISHED` is emitted.

4. **Vendor extensions arriving before the first run are buffered.** They're flushed as `CUSTOM` events when `start_run()` is called.

## Approval Flow (Detail)

ACP uses a request/response pattern for tool approvals. AG-UI has no concept of "respond to an event" — it's unidirectional. The bridge reconciles this:

```
1. Agent sends: session/request_permission (JSON-RPC request, id=42)
   Bridge stores: pending_permissions["tool_call_xyz"] = 42
   Bridge emits:  STATE_UPDATE { approval: { pending: true, callId: "tool_call_xyz", ... } }

2. Frontend shows approval dialog
   User clicks "Approve"
   Frontend sends: POST /v2/tasks/{id}/approval { callId: "tool_call_xyz", approved: true }

3. TaskManager:
   - Pops RPC ID 42 from pending_permissions
   - Calls protocol.respond(42, { outcome: { outcome: "selected", optionId: "allow_once" } })
   - Bridge emits: STATE_UPDATE { approval: { pending: false, callId: "tool_call_xyz", approved: true } }

4. Agent receives response, executes tool
   Agent sends: session/update → tool_call_update (status=completed)
   Bridge emits: TOOL_CALL_END
```

## Custom Events

Vendor-specific ACP notifications (prefixed with `_*.dev/`) are translated to `CUSTOM` AG-UI events:

| ACP Method | AG-UI Custom Name |
|------------|-------------------|
| `_kiro.dev/metadata` | `agent:metadata` |
| `_kiro.dev/mcp/server_initialized` | `agent:mcp_initialized` |
| `_kiro.dev/compaction/status` | `agent:compaction` |
| `_kiro.dev/commands/available` | `agent:commands_available` |
| `_session/terminate` | `agent:subagent_terminated` |

Other agents may use different extension prefixes — the bridge handles any `_*.dev/*` pattern.

## SSE Transport Format

Events are encoded as standard Server-Sent Events:

```
event: TEXT_MESSAGE_START
data: {"type":"TEXT_MESSAGE_START","messageId":"msg_abc","role":"assistant","timestamp":1707820801}

event: TEXT_MESSAGE_CONTENT
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg_abc","delta":"Hello! ","timestamp":1707820801}

event: TEXT_MESSAGE_CONTENT
data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg_abc","delta":"How can I help?","timestamp":1707820802}

event: TEXT_MESSAGE_END
data: {"type":"TEXT_MESSAGE_END","messageId":"msg_abc","timestamp":1707820803}

event: RUN_FINISHED
data: {"type":"RUN_FINISHED","runId":"run_xyz","taskId":"task_123","timestamp":1707820810}
```

A keepalive comment (`: keepalive\n\n`) is sent every 30 seconds if no events are pending.
