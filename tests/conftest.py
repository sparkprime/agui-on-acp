"""Pytest fixtures: in-process SessionManager + fake ACP agent + httpx client.

The fixture graph:

  event_loop (pytest-asyncio)
    └─ transport_pair (TransportPair)
        └─ fake_agent (FakeAcpAgent attached to the agent side)
            └─ session_manager (SessionManager whose AgentRunner.spawn is
                                patched to wire the ClientSideConnection to
                                the transport's client side instead of
                                spawning a subprocess)
                └─ http_client (httpx.AsyncClient against the FastAPI app)

Tests POST to ``/ag-ui`` exactly like a real AG-UI client and consume the
SSE stream, asserting on the translated events. The only thing not real is
the OS subprocess — replaced by the in-process transport pair.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio

# Make the repo root importable so `import agui_on_acp` and `import tests`
# both work from anywhere.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import acp
import httpx
from httpx import ASGITransport

from agui_on_acp.main import app as fastapi_app
from agui_on_acp.sessions.manager import SessionManager
from agui_on_acp.sessions.store import SessionStore

from tests.fake_agent import FakeAcpAgent, Script
from tests.transport import TransportPair, make_transport_pair

# Use a short permission TTL in tests so expiry paths don't take 5 minutes.
import agui_on_acp.bridge.acp_to_agui as _bridge_mod


@pytest.fixture(autouse=True)
def _short_permission_ttl() -> Iterator[None]:
    """Shrink the parked-future TTL for tests; restore afterwards."""
    original = _bridge_mod.PERMISSION_TTL_SECONDS
    _bridge_mod.PERMISSION_TTL_SECONDS = 2.0
    try:
        yield
    finally:
        _bridge_mod.PERMISSION_TTL_SECONDS = original


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture
async def transport_pair() -> AsyncIterator[TransportPair]:
    tp = make_transport_pair()
    try:
        yield tp
    finally:
        # Close both sides so no read loop lingers between tests.
        tp.client_writer.close()
        tp.agent_writer.close()
        try:
            await asyncio.wait_for(tp.client_writer.wait_closed(), timeout=1.0)
        except Exception:
            pass
        try:
            await asyncio.wait_for(tp.agent_writer.wait_closed(), timeout=1.0)
        except Exception:
            pass


@pytest_asyncio.fixture
async def fake_agent(transport_pair: TransportPair) -> AsyncIterator[FakeAcpAgent]:
    agent = FakeAcpAgent(transport_pair, script=[])
    agent.attach()
    try:
        yield agent
    finally:
        await agent.aclose()


def _patch_runner_spawn(agent: FakeAcpAgent) -> None:
    """Monkeypatch ``AgentRunner.spawn`` to use the in-process transport.

    Replaces the real subprocess spawn with one that builds a
    ``ClientSideConnection`` over the transport pair's client side. The
    bridge's ``acp.Client`` callbacks then run against the fake agent
    through real JSON-RPC framing.
    """
    from agui_on_acp.agent.runner import AgentRunner

    async def _fake_spawn(self: AgentRunner, client: acp.Client, env: dict[str, str] | None = None) -> acp.ClientSideConnection:
        from acp import ClientSideConnection  # deprecated import path the bridge uses
        # ClientSideConnection(to_client, writer, reader): the client WRITES
        # requests into client_writer (which feeds the agent's reader) and
        # READS responses from client_reader (which the agent's writer feeds).
        conn = ClientSideConnection(client, agent._transport.client_writer, agent._transport.client_reader)
        self.conn = conn
        # No subprocess — fabricate a process stand-in with a pid so
        # AgentRunner.is_alive() and kill() don't blow up.
        class _FakeProc:
            pid = 12345
            returncode: int | None = None
        self.process = _FakeProc()  # type: ignore[assignment]
        self._context_manager = None
        return conn

    async def _fake_kill(self: AgentRunner) -> None:
        # Close the client connection; nothing else to tear down.
        if self.conn is not None:
            try:
                await self.conn.close()
            except Exception:
                pass
            self.conn = None
        self.process = None

    AgentRunner.spawn = _fake_spawn  # type: ignore[assignment]
    AgentRunner.kill = _fake_kill  # type: ignore[assignment]


@pytest_asyncio.fixture
async def session_manager(fake_agent: FakeAcpAgent) -> AsyncIterator[SessionManager]:
    # Use a temp on-disk sqlite path so parallel runs don't clash.
    db_path = os.path.join("/tmp/opencode", f"test-{os.getpid()}-{id(fake_agent)}.db")
    os.makedirs("/tmp/opencode", exist_ok=True)
    store = SessionStore(db_path=db_path)
    await store.initialize()

    manager = SessionManager(store, agent_command=["fake"])
    _patch_runner_spawn(fake_agent)
    try:
        yield manager
    finally:
        await manager.shutdown()
        await store.close()
        try:
            os.remove(db_path)
        except OSError:
            pass


@pytest_asyncio.fixture
async def http_client(session_manager: SessionManager) -> AsyncIterator[httpx.AsyncClient]:
    fastapi_app.state.session_manager = session_manager
    fastapi_app.state.session_store = session_manager._store
    transport = ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
