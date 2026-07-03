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
from agui_on_acp.config import load_config
from agui_on_acp.types.api import HealthResponse

from agui_on_acp.logging_config import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

config = load_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan context manager for startup/shutdown."""
    setup_logging()  # Re-apply after uvicorn's setup
    logger.info(f"ACP → AG-UI Bridge v{__version__} (FastAPI)")
    logger.info(f"Backend: http://localhost:{config.backend_port}")
    logger.info("Endpoints:")
    skip = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    for route in app.routes:
        if hasattr(route, "methods") and hasattr(route, "path"):
            if route.path in skip:
                continue
            methods = ", ".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
            if methods:
                logger.info(f"  {methods:6s} {route.path}")
    logger.info("---")

    app.state.config = config

    from agui_on_acp.sessions.store import SessionStore
    session_store = SessionStore(db_path=config.db_path)
    await session_store.initialize()
    app.state.session_store = session_store

    from agui_on_acp.sessions.manager import SessionManager
    session_manager = SessionManager(session_store, agent_command=config.agent_command)
    app.state.session_manager = session_manager

    yield

    logger.info("Shutting down ACP → AG-UI Bridge")
    await session_manager.shutdown()
    await session_store.close()


app = FastAPI(
    title=config.display_title,
    description=config.description,
    version=__version__,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse, tags=["health"])
async def health_check() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(status="ok", version=__version__, project=config.project_name)


from agui_on_acp.sessions.routes import router as sessions_router

app.include_router(sessions_router, tags=["sessions"])

from agui_on_acp.agui_endpoint import router as agui_router

app.include_router(agui_router, tags=["ag-ui"])
