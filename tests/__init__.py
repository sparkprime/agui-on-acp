"""Test harness — in-process ACP transport + scriptable fake agent.

The bridge is a protocol translator: input boundary = AG-UI (POST /ag-ui
SSE), output boundary = ACP (JSON-RPC over stdio). To integration-test the
whole translation we replace the *only* thing we can't keep in-process —
the OS subprocess — with an in-memory asyncio stream pair, then speak the
real ACP JSON-RPC over it. The bridge's ``ClientSideConnection``, the
``AgentSideConnection``, the bridge's ``acp.Client`` callbacks, and the full
FastAPI SSE stack are all exercised exactly as in production.

Layout:
  transport.py  — ``make_transport_pair``: two ``asyncio.StreamReader``/
                  ``StreamWriter`` pairs connected by in-memory pipes.
  fake_agent.py — ``FakeAcpAgent``: a scriptable ``acp.Agent`` impl that
                  emits canned ``session_update`` notifications, fires
                  ``request_permission`` at scripted points, and records
                  every call it receives.
  conftest.py   — pytest fixtures wiring a ``SessionManager`` whose
                  ``AgentRunner.spawn`` is patched to use the in-process
                  transport instead of ``spawn_agent_process``, plus a
                  ``httpx.AsyncClient`` against the FastAPI app.
"""
