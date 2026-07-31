"""FastAPI main application for the ACP → AG-UI Bridge."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from agui_on_acp import __version__
from agui_on_acp.agui_endpoint import router as agui_router
from agui_on_acp.config import (
    agent_command,
    backend_port,
    cors_origins,
    data_dir,
    idle_ttl_seconds,
    log_the_config,
    validate_env_vars,
)
from agui_on_acp.logging_config import setup_logging
from agui_on_acp.sessions.manager import SessionManager
from agui_on_acp.sessions_endpoint import router as sessions_router

load_dotenv()

# Windows: force the Proactor event loop. uvicorn's --reload supervisor sets
# WindowsSelectorEventLoopPolicy, which doesn't implement subprocess_exec and
# breaks spawning ACP agents (NotImplementedError from _make_subprocess_transport).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

setup_logging()
logger = logging.getLogger(__name__)


async def _idle_reaper(manager: SessionManager) -> None:
    """Background task that periodically destroys idle ``ActiveSession``s.

    A session is reaped after ``idle_ttl_seconds()`` of inactivity, unless
    it has a pending permission/elicitation interrupt (a user mid-approval
    shouldn't have their subprocess killed by an unrelated timer).
    """
    while True:
        await asyncio.sleep(60)
        try:
            destroyed = await manager.sweep_idle(idle_ttl_seconds())
            if destroyed:
                logger.info("idle-reaped sessions: %s", destroyed)
        except Exception:  # pylint: disable=broad-exception-caught
            # Background task — must not crash the reaper loop.  Any failure
            # is retried on the next 60 s tick; logger.exception captures the
            # full trace so the error is visible without killing the task.
            logger.exception("idle reaper sweep failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan context manager for startup/shutdown."""
    setup_logging()  # Re-apply after uvicorn's setup
    validate_env_vars()
    log_the_config()
    logger.info("ACP on AGUI v%s", __version__)
    logger.info("Backend: http://localhost:%d", backend_port())
    logger.info("Endpoints:")
    for route in _app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        joined = ", ".join(sorted(methods - {"HEAD", "OPTIONS"}))
        if joined:
            logger.info("  %6s %s", joined, path)
    logger.info("---")

    session_manager = SessionManager(agent_command=agent_command(), data_dir=data_dir())
    _app.state.session_manager = session_manager

    reaper = asyncio.create_task(_idle_reaper(session_manager))

    yield

    logger.info("Shutting down.")
    reaper.cancel()
    try:
        await reaper
    except asyncio.CancelledError:
        pass
    await session_manager.shutdown()


app = FastAPI(
    title="AG-UI on ACP",
    description="Give any ACP-compatible coding agent an AG-UI interface",
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class HealthResponse(BaseModel):
    """Response body for health check."""

    status: Literal["ok"] = "ok"
    version: str
    project: str


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__, project="agui-on-acp")


app.include_router(agui_router, tags=["ag-ui"])
app.include_router(sessions_router, tags=["sessions"])
