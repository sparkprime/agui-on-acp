"""Persistent session metadata store.

Holds the one fact the bridge itself creates and otherwise throws away:
the ``cwd`` each ``session_id`` belongs to. Written at ``create_session``
time, read by ``connect_session``/``attach_for_prompt`` (so a client
resuming a conversation by ``threadId`` alone doesn't need to resend
``cwd``), removed on ``delete_session``.

This is deliberately *not* a general session store — no conversation
content, no run state, nothing ACP/opencode already owns better. Just the
single ``session_id → cwd`` row the bridge needs to stop leaking an ACP
implementation detail (``cwd`` as a required per-call parameter) across the
AG-UI abstraction boundary.

Records live as one JSON file per session under ``<data_dir>/sessions/``.
The base directory is configurable (``AGUI_ON_ACP_DATA_DIR``, default
``~/.agui-on-acp``); tests inject a temp directory.
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

# Session ids are filesystem-safe (opencode uses ``ses_...``); reject
# anything that isn't so a hostile or buggy id can't escape the sessions
# directory via path traversal.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class SessionStore:
    """Filesystem-backed ``session_id → cwd`` record store."""

    def __init__(self, base_dir: Path) -> None:
        self._base = base_dir / "sessions"

    def _path(self, session_id: str) -> Path:
        if not _SAFE_ID.match(session_id):
            raise ValueError(f"unsafe session id for store: {session_id!r}")
        return self._base / f"{session_id}.json"

    async def put(self, session_id: str, cwd: str) -> None:
        """Write (or overwrite) the ``cwd`` record for ``session_id``."""
        payload: dict[str, Any] = {"sessionId": session_id, "cwd": cwd}
        path = self._path(session_id)
        tmp = path.with_suffix(".json.tmp")
        await asyncio.to_thread(self._write, tmp, payload)
        # Atomic rename — a crash mid-write leaves no corrupt record.
        await asyncio.to_thread(os.replace, tmp, path)

    async def get(self, session_id: str) -> str | None:
        """Return the stored ``cwd`` for ``session_id``, or ``None`` if no
        record exists (unknown id, or a session created before this store
        shipped)."""
        path = self._path(session_id)
        if not await asyncio.to_thread(path.exists):
            return None
        try:
            data = await asyncio.to_thread(self._read, path)
        except Exception:
            logger.warning("corrupt session record at %s; ignoring", path)
            return None
        cwd = data.get("cwd")
        return cwd if isinstance(cwd, str) else None

    async def remove(self, session_id: str) -> None:
        """Delete the record for ``session_id`` (no-op if absent)."""
        path = self._path(session_id)
        if await asyncio.to_thread(path.exists):
            try:
                await asyncio.to_thread(path.unlink)
            except OSError:
                logger.warning("failed to remove session record %s", path)

    @staticmethod
    def _write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return cast(dict[str, Any], data)
        return {}
