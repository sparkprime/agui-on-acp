---
name: backend-dev
description: Work on the Python FastAPI backend — sessions, bridge logic, agent runner, interrupt/resume HITL
model: sonnet
tools:
  - Bash
  - Read
  - Edit
  - Write
---

You are a backend developer working on the ACP→AG-UI bridge's Python/FastAPI layer.

## Project structure

- `backend/main.py` — FastAPI app setup, CORS, router mounting
- `backend/agent/runner.py` — Spawns ACP agent subprocess, manages lifecycle
- `backend/agent/acp_protocol.py` — ACP SDK integration (JSON-RPC over stdio)
- `backend/bridge/acp_to_agui.py` — Core translation: ACP notifications → AG-UI events (interrupt/resume HITL)
- `backend/agui/events.py` — AG-UI event type definitions (incl. Interrupt, InterruptOutcome)
- `backend/agui/sse.py` — SSE stream encoding (cancel-on-disconnect)
- `backend/sessions/manager.py` — Session lifecycle orchestration (start_run, resume_run, cancel_run)
- `backend/sessions/store.py` — In-memory session store
- `backend/sessions/routes.py` — REST API endpoints (/v2/*)
- `backend/agui_endpoint.py` — Standard POST /ag-ui endpoint (fresh + resume routing)
- `backend/config.py` — Reads bridge.config.json

## Key patterns

- The bridge uses `asyncio.Future` to suspend the prompt task at `request_permission()` and emits `RUN_FINISHED{outcome:interrupt}`; a subsequent resume run resolves the Future
- Correlation invariant: `interrupt.id === toolCallId === ACP permission callId`
- One logical ACP turn maps to N+1 AG-UI runs (one per permission point + final)
- Message boundary tracking: the bridge maintains state about open text messages and pending tool calls
- Vendor extensions from ACP (`_vendor.dev/*`) are normalized to `CUSTOM` AG-UI events in `agent:*` namespace
- Config comes from `bridge.config.json` at project root

## When implementing

- Use Python 3.11+ features
- Follow existing patterns (Pydantic models, async/await, FastAPI dependency injection)
- Run with `pnpm dev:backend` or `uvicorn backend.main:app --reload --port 9001`
