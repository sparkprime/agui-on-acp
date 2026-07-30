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
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

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

# Use a short permission TTL in tests so expiry paths don't take 5 minutes.
import agui_on_acp.bridge.acp_to_agui as _bridge_mod
from agui_on_acp.main import app as fastapi_app
from agui_on_acp.sessions.manager import SessionManager
from tests.fake_agent import FakeAcpAgent, FakeSessionStore
from tests.transport import TransportPair, make_transport_pair


@pytest.fixture(autouse=True)
def short_permission_ttl() -> Iterator[None]:
    """Shrink the parked-future TTL for tests; restore afterwards."""
    original = _bridge_mod.PERMISSION_TTL_SECONDS
    _bridge_mod.PERMISSION_TTL_SECONDS = 2.0
    try:
        yield
    finally:
        _bridge_mod.PERMISSION_TTL_SECONDS = original


@pytest.fixture(autouse=True)
def permissive_cwd_allowlist(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Allow any cwd in tests (the security lockdown is exercised
    separately via direct ``is_cwd_allowed`` calls)."""
    monkeypatch.setenv("AGUI_ON_ACP_ALLOWED_CWD_PREFIXES", "/")
    yield


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

    async def _fake_spawn(
        self: AgentRunner, client: acp.Client, env: dict[str, str] | None = None
    ) -> acp.ClientSideConnection:
        from acp import ClientSideConnection  # deprecated import path the bridge uses

        # ClientSideConnection(to_client, writer, reader): the client WRITES
        # requests into client_writer (which feeds the agent's reader) and
        # READS responses from client_reader (which the agent's writer feeds).
        conn = ClientSideConnection(
            client,
            agent.transport.client_writer,
            agent.transport.client_reader,
            # Match the real runner: enable the unstable protocol so ACP 0.11
            # routes (elicitation_create, etc.) are accepted, not rejected
            # with method_not_found.
            use_unstable_protocol=True,
        )
        self.conn = conn

        # No subprocess — fabricate a process stand-in with a pid so
        # AgentRunner.is_alive() and kill() don't blow up.
        class _FakeProc:
            pid = 12345
            returncode: int | None = None

        self.process = _FakeProc()  # type: ignore[assignment]
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
async def session_manager(
    fake_agent: FakeAcpAgent, tmp_path: Path
) -> AsyncIterator[SessionManager]:
    manager = SessionManager(agent_command=["fake"], data_dir=str(tmp_path))
    _patch_runner_spawn(fake_agent)
    try:
        yield manager
    finally:
        await manager.shutdown()


@pytest_asyncio.fixture
async def precreated_session_id(
    session_manager: SessionManager,
    fake_agent: FakeAcpAgent,
) -> AsyncIterator[str]:
    """Pre-create one live session for translation tests.

    Under the attach-only ``POST /ag-ui`` contract the caller must already
    have a session; the translation tests aren't exercising Create/Connect,
    so this fixture front-loads a single ``create_session`` (which the fake
    mints as ``fake-session-1``) and yields the id for tests to use as
    ``threadId``. The new session-lifecycle tests build their own stacks
    instead of depending on this fixture.
    """
    active = await session_manager.create_session(cwd="/tmp/opencode")
    yield active.session_id


@pytest_asyncio.fixture
async def http_client(
    session_manager: SessionManager,
    precreated_session_id: str,
) -> AsyncIterator[httpx.AsyncClient]:
    fastapi_app.state.session_manager = session_manager
    transport = ASGITransport(app=fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── Stack-builder for capability / restart tests ────────────────────────────


async def make_stack(
    *,
    capabilities_opts: acp.schema.AgentCapabilities | None = None,
    store: FakeSessionStore | None = None,
    script: list[Any] | None = None,
    data_dir: str | None = None,
) -> tuple[FakeAcpAgent, SessionManager, httpx.AsyncClient]:
    """Construct a fresh fake-agent + transport + manager + httpx client.

    Used by the session-lifecycle / restart tests that need to configure
    capabilities or share a store across two fake instances. Each call
    re-patches ``AgentRunner.spawn`` to the most recently built agent (so
    a second call for a "restart" test rewires the manager to agent2 —
    fine, since manager1 is discarded by then).

    ``data_dir`` is the bridge's persistent-state directory (the per-session
    ``cwd`` record store). When omitted a fresh temp dir is created and
    cleaned up by ``teardown_stack``; pass an explicit path (shared across
    two stacks) to model "the bridge restarted but its on-disk store
    survived" — the caller then owns cleaning that dir up.
    """
    owns_tmp = data_dir is None
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if owns_tmp:
        tmp = tempfile.TemporaryDirectory()
        data_dir = tmp.name
    tp = make_transport_pair()
    agent = FakeAcpAgent(
        tp, script=script or [], capabilities=capabilities_opts, store=store
    )
    agent.attach()
    _patch_runner_spawn(agent)
    manager = SessionManager(agent_command=["fake"], data_dir=data_dir)
    if tmp is not None:
        # Stash for teardown_stack to clean up only when we own it.
        manager._test_tmp = tmp  # type: ignore[attr-defined]
    fastapi_app.state.session_manager = manager
    transport = ASGITransport(app=fastapi_app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    await client.__aenter__()
    return agent, manager, client


async def teardown_stack(
    agent: FakeAcpAgent, manager: SessionManager, client: httpx.AsyncClient
) -> None:
    await client.aclose()
    await manager.shutdown()
    await agent.aclose()
    agent.transport.client_writer.close()
    agent.transport.agent_writer.close()
    tmp = getattr(manager, "_test_tmp", None)
    if tmp is not None:
        tmp.cleanup()
