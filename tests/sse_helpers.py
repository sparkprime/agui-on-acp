"""Helpers for consuming AG-UI SSE streams in tests.

``httpx.AsyncClient.stream`` gives us raw bytes; AG-UI events are
``event: <TYPE>\\ndata: <json>\\n\\n`` frames. These helpers parse them into
typed dicts so tests can assert cleanly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

__all__ = ["read_sse_events", "read_until", "event_of_type", "parse_sse_frame"]


def parse_sse_frame(frame: str) -> dict[str, Any] | None:
    """Parse one SSE frame (possibly multiple ``data:`` lines) into a dict.

    Returns ``None`` for keepalive comments (``: keepalive\\n\\n``).
    """
    if frame.startswith(":"):
        return None
    event_type: str | None = None
    data_lines: list[str] = []
    for line in frame.splitlines():
        if not line:
            continue
        if line.startswith("event: "):
            event_type = line[len("event: "):]
        elif line.startswith("data: "):
            data_lines.append(line[len("data: "):])
    if event_type is None or not data_lines:
        return None
    payload = json.loads("\n".join(data_lines))
    return {"type": event_type, "data": payload}


async def read_sse_events(
    response: httpx.Response,
    *,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Read an SSE response fully until the stream ends, return parsed events.

    The bridge's SSE streams terminate after ``RUN_FINISHED`` /
    ``RUN_ERROR`` (sse.py:37-39), so this returns naturally. Keepalive
    comments are filtered out.
    """
    events: list[dict[str, Any]] = []
    buf = ""
    async for chunk in response.aiter_bytes():
        buf += chunk.decode("utf-8", errors="replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            parsed = parse_sse_frame(frame)
            if parsed is not None:
                events.append(parsed)
    # Trailing frame without a blank-line terminator
    if buf.strip():
        parsed = parse_sse_frame(buf)
        if parsed is not None:
            events.append(parsed)
    return events


async def read_until(
    response: httpx.Response,
    types: set[str],
    *,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Read events until one of ``types`` appears (inclusive). Returns them."""
    out: list[dict[str, Any]] = []
    buf = ""
    deadline = asyncio.get_event_loop().time() + timeout
    async for chunk in response.aiter_bytes():
        buf += chunk.decode("utf-8", errors="replace")
        while "\n\n" in buf:
            frame, buf = buf.split("\n\n", 1)
            parsed = parse_sse_frame(frame)
            if parsed is not None:
                out.append(parsed)
                if parsed["type"] in types:
                    return out
        if asyncio.get_event_loop().time() > deadline:
            break
    return out


def event_of_type(events: list[dict[str, Any]], t: str) -> dict[str, Any]:
    for e in events:
        if e["type"] == t:
            return e
    raise AssertionError(f"no event of type {t} in {[e['type'] for e in events]}")
