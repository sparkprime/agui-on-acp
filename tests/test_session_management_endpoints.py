"""Integration tests for the session CRUD endpoints.

``GET /ag-ui/sessions``, ``DELETE /ag-ui/sessions/{id}``,
``GET /ag-ui/capabilities``. The key distinction these guard: ``delete``
removes the persisted record; ``close`` does not (PLAN3 item 6).
"""

import pytest

from agui_on_acp.sessions.manager import CwdRecordNotFoundError
from tests.conftest import make_stack, teardown_stack
from tests.fake_agent import capabilities

CWD = "/tmp/opencode"


@pytest.mark.asyncio
async def test_list_sessions_proxies_and_reflects_store():
    """GET /ag-ui/sessions proxies to the agent and supports cwd filtering."""
    fake, manager, client = await make_stack(capabilities_opts=capabilities(list_=True))
    try:
        fake.store.create(CWD)
        fake.store.create("/repo")
        resp = await client.get("/ag-ui/sessions")
        assert resp.status_code == 200
        body = resp.json()
        ids = {s["sessionId"] for s in body["sessions"]}
        assert "fake-session-1" in ids
        assert "fake-session-2" in ids
        # Filter by cwd.
        resp = await client.get(f"/ag-ui/sessions?cwd={CWD}")
        ids = {s["sessionId"] for s in resp.json()["sessions"]}
        assert ids == {"fake-session-1"}
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_list_sessions_survives_restart():
    """A fresh subprocess (manager2) backed by the SAME store still lists
    the sessions created under manager1."""
    fake1, manager1, client1 = await make_stack(
        capabilities_opts=capabilities(list_=True)
    )
    shared_store = fake1.store
    fake1.store.create(CWD)
    await teardown_stack(fake1, manager1, client1)

    # "The bridge restarts": a brand-new fake+manager over the same store.
    fake2, manager2, client2 = await make_stack(
        capabilities_opts=capabilities(list_=True), store=shared_store
    )
    try:
        resp = await client2.get("/ag-ui/sessions")
        assert resp.status_code == 200
        ids = {s["sessionId"] for s in resp.json()["sessions"]}
        assert "fake-session-1" in ids
    finally:
        await teardown_stack(fake2, manager2, client2)


@pytest.mark.asyncio
async def test_list_unsupported_is_501():
    """Listing when session/list is unsupported returns 501."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(list_=False)
    )
    try:
        resp = await client.get("/ag-ui/sessions")
        assert resp.status_code == 501
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_delete_removes_cwd_record():
    """DELETE removes the session from the agent's list AND drops the
    bridge's own ``session_id → cwd`` record (so it doesn't accumulate
    rows for sessions that no longer exist)."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(list_=True, delete=True)
    )
    try:
        # Create via the manager so the bridge writes a cwd record.
        active = await manager.create_session(cwd=CWD)
        sid = active.session_id
        assert await manager.resolve_cwd(sid) == CWD

        resp = await client.delete(f"/ag-ui/sessions/{sid}")
        assert resp.status_code == 204
        assert sid in fake.delete_session_calls

        # Gone from the agent's list…
        ids = {
            s["sessionId"]
            for s in (await client.get("/ag-ui/sessions")).json()["sessions"]
        }
        assert sid not in ids
        # …and the bridge's own cwd record is gone too.
        with pytest.raises(CwdRecordNotFoundError):
            await manager.resolve_cwd(sid)
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_delete_unsupported_is_501():
    """Deleting when session/delete is unsupported returns 501."""
    fake, manager, client = await make_stack(
        capabilities_opts=capabilities(delete=False, list_=True)
    )
    try:
        s = fake.store.create(CWD)
        resp = await client.delete(f"/ag-ui/sessions/{s.session_id}")
        assert resp.status_code == 501
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_capabilities_endpoint_reflects_fake_agent_capabilities():
    """GET /ag-ui/capabilities reflects the agent's advertised capabilities."""
    caps = capabilities(load_session=True, resume=True, list_=True, delete=True)
    fake, manager, client = await make_stack(capabilities_opts=caps)
    try:
        resp = await client.get("/ag-ui/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        assert body["loadSession"] is True
        sc = body["sessionCapabilities"]
        assert sc["resume"] is True
        assert sc["list"] is True
        assert sc["delete"] is True
    finally:
        await teardown_stack(fake, manager, client)


@pytest.mark.asyncio
async def test_capabilities_probed_once_and_cached():
    """First GET /ag-ui/capabilities probes (initialize once); second is a
    cache hit (no extra initialize)."""
    fake, manager, client = await make_stack()
    try:
        before = len(fake.initialize_calls)
        r1 = await client.get("/ag-ui/capabilities")
        assert r1.status_code == 200
        after_first = len(fake.initialize_calls)
        assert after_first == before + 1, "first call must probe exactly once"

        r2 = await client.get("/ag-ui/capabilities")
        assert r2.status_code == 200
        after_second = len(fake.initialize_calls)
        assert after_second == after_first, "second call must hit the cache"
    finally:
        await teardown_stack(fake, manager, client)
