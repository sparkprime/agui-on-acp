"""End-to-end restart scenarios — the two-fake-agent-instance patterns the
existing harness couldn't express before the FakeSessionStore refactor.

Persistence is modeled by a store object outliving the fake
agent/transport/manager triple, not by any single component.
"""

from __future__ import annotations

import shutil
import tempfile
from typing import Any

import pytest

from tests.conftest import make_stack, teardown_stack
from tests.fake_agent import capabilities, end_turn, request_permission, text
from tests.sse_helpers import read_sse_events, read_until

CWD = "/tmp/opencode"


def _prompt_body(sid: str, content: str = "again") -> dict[str, Any]:
    # No ``cwd`` in forwardedProps — the bridge resolves it from its
    # durable ``session_id → cwd`` record, which is the whole point of
    # the cwd-persistence change this restart test now also exercises.
    return {
        "threadId": sid,
        "runId": "r1",
        "messages": [{"role": "user", "id": "u1", "content": content}],
        "forwardedProps": {},
    }


@pytest.mark.asyncio
async def test_resume_after_bridge_restart_continues_same_session_id():
    """Create under manager1 → prompt → discard manager1 → prompt again
    under manager2 with the same threadId → succeeds via resume_session on
    agent2; threadId unchanged throughout.

    Both the fake agent's store AND the bridge's on-disk cwd record survive
    the restart (shared ``store`` + shared ``data_dir``)."""
    shared_data_dir = tempfile.mkdtemp()
    try:
        fake1, manager1, client1 = await make_stack(
            capabilities_opts=capabilities(resume=True), data_dir=shared_data_dir
        )
        shared_store = fake1.store
        fake1.script = [text("first"), end_turn()]
        active = await manager1.create_session(cwd=CWD)
        sid = active.session_id
        async with client1.stream(
            "POST", "/ag-ui", json=_prompt_body(sid, "first")
        ) as resp:
            events1 = await read_sse_events(resp)
        assert any(e["type"] == "RUN_FINISHED" for e in events1)
        await teardown_stack(fake1, manager1, client1)

        # The bridge "restarts": fresh fake+transport+manager, same fake
        # store AND same bridge data_dir (cwd record survives on disk).
        fake2, manager2, client2 = await make_stack(
            capabilities_opts=capabilities(resume=True),
            store=shared_store,
            data_dir=shared_data_dir,
        )
        try:
            fake2.script = [text("second"), end_turn()]
            async with client2.stream(
                "POST", "/ag-ui", json=_prompt_body(sid, "second")
            ) as resp:
                assert resp.status_code == 200
                events2 = await read_sse_events(resp)
            # Resumed on agent2 (agent1 is gone) — never created anew.
            assert len(fake2.resume_session_calls) == 1
            assert fake2.resume_session_calls[0]["session_id"] == sid
            assert fake2.new_session_calls == []
            # threadId is unchanged throughout.
            started = [e for e in events2 if e["type"] == "RUN_STARTED"][0]
            assert started["data"]["threadId"] == sid
        finally:
            await teardown_stack(fake2, manager2, client2)
    finally:
        shutil.rmtree(shared_data_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_permission_interrupt_orphaned_by_restart_yields_clear_error_not_hang():
    """A permission interrupt parked in manager1's bridge is lost when
    manager1 is discarded; resuming it against manager2 (which has no
    ActiveSession at all) must surface a clear error rather than hanging
    or 500ing."""
    shared_data_dir = tempfile.mkdtemp()
    try:
        fake1, manager1, client1 = await make_stack(
            capabilities_opts=capabilities(resume=True), data_dir=shared_data_dir
        )
        shared_store = fake1.store
        fake1.script = [request_permission("perm1"), end_turn()]
        active = await manager1.create_session(cwd=CWD)
        sid = active.session_id
        async with client1.stream("POST", "/ag-ui", json=_prompt_body(sid)) as resp:
            await read_until(resp, {"RUN_FINISHED"})
        # Discard manager1 WITHOUT resolving the interrupt — the parked Future
        # lives only in manager1's bridge.
        await teardown_stack(fake1, manager1, client1)

        fake2, manager2, client2 = await make_stack(
            capabilities_opts=capabilities(resume=True),
            store=shared_store,
            data_dir=shared_data_dir,
        )
        try:
            # Attempt to resume the orphaned interrupt against manager2.
            # manager2 has no ActiveSession (and no parked Future), so this
            # is a pre-stream failure → JSON 404, not an SSE RUN_ERROR.
            resume_body = _prompt_body(sid, content="")
            resume_body["resume"] = [{"interruptId": "perm1", "status": "resolved"}]
            resp = await client2.post("/ag-ui", json=resume_body)
            assert resp.status_code == 404
            assert "error" in resp.json()
        finally:
            await teardown_stack(fake2, manager2, client2)
    finally:
        shutil.rmtree(shared_data_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_list_and_delete_operate_on_whichever_subprocess_is_convenient():
    """After a restart, GET /ag-ui/sessions and DELETE work via a fresh
    probe against agent2 — no need for manager1/agent1 to exist."""
    fake1, manager1, client1 = await make_stack(
        capabilities_opts=capabilities(list_=True, delete=True)
    )
    shared_store = fake1.store
    fake1.store.create(CWD)
    await teardown_stack(fake1, manager1, client1)

    fake2, manager2, client2 = await make_stack(
        capabilities_opts=capabilities(list_=True, delete=True), store=shared_store
    )
    try:
        resp = await client2.get("/ag-ui/sessions")
        assert resp.status_code == 200
        ids = {s["sessionId"] for s in resp.json()["sessions"]}
        assert "fake-session-1" in ids

        dele = await client2.delete("/ag-ui/sessions/fake-session-1")
        assert dele.status_code == 204
        ids_after = {
            s["sessionId"]
            for s in (await client2.get("/ag-ui/sessions")).json()["sessions"]
        }
        assert "fake-session-1" not in ids_after
    finally:
        await teardown_stack(fake2, manager2, client2)
