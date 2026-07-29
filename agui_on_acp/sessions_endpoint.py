"""Session-lifecycle endpoints: Create / Connect / List / Delete /
Capabilities.

Split out of ``agui_endpoint.py`` because the session CRUD surface
(``/ag-ui/sessions*``, ``/ag-ui/capabilities``) is a distinct concern from
the run surface (``/ag-ui``, ``/ag-ui/config``).

Three operations back the AG-UI conversation lifecycle (PLAN3 §"Core
design"):

  * ``POST /ag-ui/sessions``            — Create (``session/new``)
  * ``GET  /ag-ui/sessions/{id}/connect`` — Connect (``session/load``,
                                             replays history as a
                                             ``MESSAGES_SNAPSHOT``)
  * ``POST /ag-ui`` (in agui_endpoint)  — Prompt (``session/prompt`` after
                                             ``attach_for_prompt``)

Plus the management side: ``GET /ag-ui/sessions``, ``DELETE
/ag-ui/sessions/{id}``, and ``GET /ag-ui/capabilities``.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from agui_on_acp.agui.sse import event_stream
from agui_on_acp.config import is_cwd_allowed
from agui_on_acp.sessions.manager import (
    DeleteUnsupportedError,
    ListUnsupportedError,
    LoadSessionUnsupportedError,
    SessionNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateSessionRequest(BaseModel):
    cwd: str
    mode: str | None = None
    model: str | None = None
    mcpServers: dict[str, Any] | None = None
    configOptions: dict[str, Any] | None = None


class CreateSessionResponse(BaseModel):
    sessionId: str
    modes: list[dict[str, str]] | None = None
    models: list[dict[str, str]] | None = None
    configOptions: list[dict[str, Any]] | None = None
    currentModeId: str | None = None


@router.post("/ag-ui/sessions", status_code=201, response_model=CreateSessionResponse)
async def create_session(body: CreateSessionRequest, request: Request) -> CreateSessionResponse:
    """Create a new conversation (``session/new``).

    Returns the session id the client must use as ``threadId`` on
    subsequent ``POST /ag-ui`` runs. Never resumes or loads — those are
    separate, explicit operations.
    """
    manager = request.app.state.session_manager
    if not is_cwd_allowed(body.cwd):
        raise HTTPException(403, "cwd not allowed")
    active = await manager.create_session(
        cwd=body.cwd,
        mode=body.mode,
        model=body.model,
        mcp_servers=body.mcpServers,
        config_options=body.configOptions,
    )
    return CreateSessionResponse(
        sessionId=active.session_id,
        modes=active.modes,
        models=active.models,
        configOptions=active.config_options,
        currentModeId=active.current_mode_id,
    )


@router.get("/ag-ui/sessions/{session_id}/connect")
async def connect_session(session_id: str, request: Request, cwd: str = "."):
    """Connect to (replay) an existing conversation.

    GET (not POST): it's a read-only replay of existing state, no body beyond
    query params, and ``EventSource`` in a browser can only do GET. Streams
    the replayed history as a ``MESSAGES_SNAPSHOT`` framed by a synthetic
    ``RUN_STARTED`` / ``RUN_FINISHED`` pair.
    """
    manager = request.app.state.session_manager
    if not is_cwd_allowed(cwd):
        return _error_stream("cwd not allowed", status_code=403)
    try:
        _active, replay_queue = await manager.connect_session(session_id, cwd)
    except LoadSessionUnsupportedError:
        return _error_stream("loadSession not supported by this agent", status_code=501)
    except SessionNotFoundError:
        return _error_stream(f"no session {session_id}", status_code=404)

    async def _on_disconnect() -> None:
        await manager.stop(session_id)

    return StreamingResponse(
        event_stream(replay_queue, session_id, on_cancel=_on_disconnect),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/ag-ui/sessions")
async def list_sessions(
    request: Request, cwd: str | None = None, cursor: str | None = None
) -> dict[str, Any]:
    manager = request.app.state.session_manager
    try:
        result = await manager.list_sessions(cwd=cwd, cursor=cursor)
    except ListUnsupportedError:
        raise HTTPException(501, "session/list not supported by this agent")
    sessions = [_serialize_session_info(s) for s in result.sessions]
    response: dict[str, Any] = {
        "sessions": sessions,
        "nextCursor": result.next_cursor,
    }
    return response


@router.delete("/ag-ui/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request) -> None:
    manager = request.app.state.session_manager
    try:
        await manager.delete_session(session_id)
    except DeleteUnsupportedError:
        raise HTTPException(501, "session/delete not supported by this agent")


@router.get("/ag-ui/capabilities")
async def get_capabilities(request: Request) -> dict[str, Any]:
    manager = request.app.state.session_manager
    caps = await manager.get_capabilities()
    sc = caps.session_capabilities
    return {
        "loadSession": bool(caps.load_session),
        "sessionCapabilities": {
            "resume": bool(sc and sc.resume),
            "list": bool(sc and sc.list),
            "delete": bool(sc and sc.delete),
        },
    }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _serialize_session_info(info: Any) -> dict[str, Any]:
    """Serialize a ``SessionInfo`` SDK model to the wire shape."""
    dump: Any = None
    if hasattr(info, "model_dump"):
        try:
            dump = info.model_dump(by_alias=True, mode="json", exclude_none=True)
        except Exception:
            dump = None
    result: dict[str, Any]
    if isinstance(dump, dict):
        result = cast(dict[str, Any], dump)
    else:
        result = {"sessionId": str(info)}
    return result


def _error_stream(message: str, *, status_code: int = 500) -> StreamingResponse:
    """Yield a single ``RUN_ERROR`` SSE event with the given status code."""
    import json

    error_event = {
        "type": "RUN_ERROR",
        "timestamp": time.time(),
        "message": message,
        "runId": str(uuid.uuid4()),
        "taskId": "error",
    }

    async def _gen():
        yield f"event: RUN_ERROR\ndata: {json.dumps(error_event)}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        status_code=status_code,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
