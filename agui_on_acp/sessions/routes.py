"""Session routes — FastAPI router for AG-UI session/task management.

Endpoints handle task CRUD, run lifecycle, and SSE streaming.
Routes keep the /v2/tasks prefix for backward compatibility.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from agui_on_acp.agui.sse import event_stream
from agui_on_acp.sessions.types import (
    CreateTaskRequest,
    CreateTaskResponse,
    ExecuteCommandRequest,
    SetModelRequest,
    SetModeRequest,
    StartRunRequest,
    StartRunResponse,
    TaskListResponse,
    TaskSummary,
    UpdateTaskRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v2")


# ── Helper to get SessionStore from app state ─────────────────────────────────


def _get_store(request: Request):
    """Retrieve SessionStore from app.state (set during lifespan startup)."""
    store = getattr(request.app.state, "session_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="SessionStore not initialized")
    return store


def _get_manager(request: Request):
    """Retrieve SessionManager from app.state (set during lifespan startup).

    Returns None if not yet wired.
    """
    return getattr(request.app.state, "session_manager", None)


# ── Task CRUD ───────────────────────────────────────────────────────────────


@router.post("/tasks", response_model=CreateTaskResponse)
async def create_task(body: CreateTaskRequest, request: Request):
    """Create a new task and spawn an agent process."""
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session manager not available")

    task_id = str(uuid.uuid4())
    active = await manager.create_task(
        task_id=task_id,
        cwd=body.cwd,
        title=body.title or "New Task",
        resume_session_id=body.resumeSessionId,
        mode=body.mode,
        model=body.model,
        mcp_servers=body.mcpServers,
        agent_command=body.agentCommand,
    )
    return CreateTaskResponse(
        taskId=active.task_id,
        agentSessionId=active.agent_session_id,
        runUrl=f"/v2/tasks/{active.task_id}/run",
        eventsUrl=f"/v2/tasks/{active.task_id}/events",
        modes=active.modes,
        models=active.models,
        currentModeId=active.current_mode_id,
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(request: Request):
    """List all tasks."""
    store = _get_store(request)
    tasks = await store.list_all()
    return TaskListResponse(tasks=tasks)


@router.get("/tasks/resumable", response_model=TaskListResponse)
async def list_resumable_tasks(request: Request):
    """List tasks that can be resumed."""
    store = _get_store(request)
    tasks = await store.list_all()
    return TaskListResponse(tasks=tasks)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: str, body: UpdateTaskRequest, request: Request):
    """Update task metadata (e.g., title)."""
    store = _get_store(request)
    try:
        task = await store.update(task_id, title=body.title)
        return task
    except ValueError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")


@router.post("/tasks/{task_id}/cancel")
async def cancel_task_run(task_id: str, request: Request):
    """Cancel the current run for a task via ACP session/cancel."""
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session manager not available")
    try:
        await manager.cancel_run(task_id)
        return {"success": True, "taskId": task_id}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"No active session: {task_id}")


@router.post("/tasks/{task_id}/stop")
async def stop_task(task_id: str, request: Request):
    """Stop a task's agent process without deleting the task.

    The task remains in the store and can be revived later via start_run.
    Use this to free resources for idle sessions while preserving history.
    """
    store = _get_store(request)
    manager = _get_manager(request)

    task = await store.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    stopped = False
    if manager is not None:
        stopped = await manager.stop(task_id)

    return {"success": True, "taskId": task_id, "wasStopped": stopped}


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, request: Request):
    """Delete a task and kill its ACP process if running."""
    store = _get_store(request)
    manager = _get_manager(request)

    if manager is not None:
        await manager.destroy(task_id)

    deleted = await store.delete(task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"success": True, "taskId": task_id}


# ── Run lifecycle ───────────────────────────────────────────────────────────


@router.post("/tasks/{task_id}/run", response_model=StartRunResponse)
async def start_run(task_id: str, body: StartRunRequest, request: Request):
    """Start a new run (send prompt to agent).

    Sends prompt via ACP, bridge translates to AG-UI events.
    """
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session manager not available")

    try:
        run_id = await manager.start_run(task_id, body.input, body.config)
    except KeyError:
        raise HTTPException(
            status_code=409,
            detail=f"Task {task_id} is no longer active. Please create a new task.",
        )
    return StartRunResponse(runId=run_id)


@router.get("/tasks/{task_id}/events")
async def stream_events(
    task_id: str,
    request: Request,
    runId: str = Query(..., description="Run ID to stream events for"),
):
    """Stream AG-UI events for a run via SSE.

    Returns a text/event-stream response. The stream closes after
    RUN_FINISHED or RUN_ERROR is sent.
    """
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="Session manager not available")

    queue = manager.get_event_queue(task_id, runId)
    if queue is None:
        raise HTTPException(
            status_code=404, detail=f"No active run {runId} for task {task_id}"
        )
    return StreamingResponse(
        event_stream(queue, task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Mode / Model / Command ──────────────────────────────────────────────────


@router.post("/tasks/{task_id}/mode")
async def set_mode(task_id: str, body: SetModeRequest, request: Request):
    """Switch agent mode for a task."""
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="SessionManager not initialized")
    try:
        await manager.set_mode(task_id, body.modeId)
        return {"success": True, "modeId": body.modeId}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    except Exception as exc:
        logger.error("Failed to set mode for task %s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/tasks/{task_id}/model")
async def set_model(task_id: str, body: SetModelRequest, request: Request):
    """Switch model for a task."""
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="SessionManager not initialized")
    try:
        await manager.set_model(task_id, body.modelId)
        return {"success": True, "modelId": body.modelId}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    except Exception as exc:
        logger.error("Failed to set model for task %s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/tasks/{task_id}/command")
async def execute_command(task_id: str, body: ExecuteCommandRequest, request: Request):
    """Execute a slash command on a task."""
    manager = _get_manager(request)
    if manager is None:
        raise HTTPException(status_code=503, detail="SessionManager not initialized")
    try:
        await manager.execute_command(task_id, body.command, body.args)
        return {"success": True, "command": body.command}
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    except Exception as exc:
        logger.error("Failed to execute command for task %s: %s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
