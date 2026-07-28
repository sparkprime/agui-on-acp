"""FastAPI main application for the ACP → AG-UI Bridge."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

# Windows: force the Proactor event loop. uvicorn's --reload supervisor sets
# WindowsSelectorEventLoopPolicy, which doesn't implement subprocess_exec and
# breaks spawning ACP agents (NotImplementedError from _make_subprocess_transport).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agui_on_acp import __version__
from agui_on_acp.config import (
    agent_command,
    backend_port,
    cors_origins,
    description,
    display_title,
    log_the_config,
    project_name,
    validate_env_vars,
)
from agui_on_acp.logging_config import setup_logging
from agui_on_acp.types.api import HealthResponse

setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager for startup/shutdown."""
    setup_logging()  # Re-apply after uvicorn's setup
    validate_env_vars()
    log_the_config()
    logger.info(f"ACP → AG-UI Bridge v{__version__} (FastAPI)")
    logger.info(f"Backend: http://localhost:{backend_port()}")
    logger.info("Endpoints:")
    skip = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue
        if path in skip:
            continue
        joined = ", ".join(sorted(methods - {"HEAD", "OPTIONS"}))
        if joined:
            logger.info(f"  {joined:6s} {path}")
    logger.info("---")

    from agui_on_acp.sessions.manager import SessionManager

    session_manager = SessionManager(agent_command=agent_command())
    app.state.session_manager = session_manager

    yield

    logger.info("Shutting down ACP → AG-UI Bridge")
    await session_manager.shutdown()


app = FastAPI(
    title=display_title(),
    description=description(),
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


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__, project=project_name())


from agui_on_acp.agui_endpoint import router as agui_router

app.include_router(agui_router, tags=["ag-ui"])
