"""AgentRunner — thin wrapper around the acp SDK's spawn_agent_process.

Manages a single ACP agent subprocess via the official agent-client-protocol
SDK. Spawns the agent, holds the ClientSideConnection and subprocess reference.
"""

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys

# asyncio.subprocess.Process is not directly importable as a name in some
# pylint versions; re-export for the type annotation below.
from asyncio.subprocess import Process as _AsyncSubprocessProcess
from typing import Any

import acp

logger = logging.getLogger(__name__)


def _kill_process_tree(root_pid: int) -> None:
    """Kill all descendants of root_pid, then root_pid itself."""
    ps_path = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.check_output([ps_path, "-eo", "pid,ppid"], text=True)
    except (OSError, subprocess.SubprocessError):
        try:
            os.kill(root_pid, signal.SIGKILL)
        except OSError:
            pass
        return

    children: dict[int, list[int]] = {}
    for line in out.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)

    def _kill_recursive(pid: int) -> None:
        for child in children.get(pid, []):
            _kill_recursive(child)
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    _kill_recursive(root_pid)


def _resolve_windows_command(command: list[str]) -> list[str]:
    """On Windows, wrap shim-only executables (.cmd/.bat/.ps1) with cmd.exe /c.

    asyncio.create_subprocess_exec calls Windows CreateProcess, which only
    runs .exe files. Tools like npx, claude-agent-acp, opencode, and many
    globally-installed npm bins ship as .cmd shims and fail with
    FileNotFoundError unless invoked through a shell.

    Returns the command unchanged on non-Windows, or when the first arg
    already resolves to a .exe (or to nothing — we let the original error
    surface instead of hiding a typo behind cmd.exe).
    """
    if sys.platform != "win32" or not command:
        return command

    first = command[0]
    # Already shell-wrapped — caller knows what they're doing.
    if first.lower() in (
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
    ):
        return command

    resolved = shutil.which(first)
    if resolved is None:
        return command

    if resolved.lower().endswith(".exe"):
        return command

    return ["cmd.exe", "/c", *command]


class AgentRunner:
    """Manages a single ACP agent subprocess via the official SDK.

    Attributes:
        task_id: Identifier of the owning task/session.
        conn: The SDK's ClientSideConnection (available after spawn).
        process: The subprocess reference (available after spawn).
    """

    def __init__(self, task_id: str, command: list[str]) -> None:
        self.task_id = task_id
        self._command = command
        self._log = logging.LoggerAdapter(logger, {"task_id": task_id})

        self.conn: acp.ClientSideConnection | None = None
        self.process: _AsyncSubprocessProcess | None = None
        self._context_manager: Any = None

    async def spawn(
        self, client: acp.Client, env: dict[str, str] | None = None
    ) -> acp.ClientSideConnection:
        """Spawn the ACP agent using the SDK's spawn_agent_process.

        Args:
            client: An acp.Client implementation that handles callbacks.
            env: Optional additional environment variables.

        Returns:
            The ClientSideConnection for making protocol calls.
        """
        # Set up environment
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        effective_command = _resolve_windows_command(self._command)
        if (
            effective_command is not self._command
            and effective_command != self._command
        ):
            self._log.info(
                "wrapped non-.exe agent shim with cmd.exe: %s",
                " ".join(effective_command),
            )

        binary = effective_command[0]
        args = effective_command[1:]

        self._context_manager = acp.spawn_agent_process(
            client,
            binary,
            *args,
            env=proc_env,
            transport_kwargs={
                "limit": 16 * 1024 * 1024
            },  # 16 MB buffer for large responses
            # Elicitation (create_elicitation / complete_elicitation) and
            # other 0.11 routes are flagged "unstable" by the SDK; enabling
            # the unstable protocol accepts them without a per-call warning.
            use_unstable_protocol=True,
        )
        # Enter the context manager manually (instead of ``async with``) so
        # the process stays alive across ``spawn()`` and ``kill()`` calls.
        # pylint: disable=unnecessary-dunder-call,no-member
        # The context manager is typed as Any (the SDK returns an
        # AsyncGeneratorContextManager that pylint can't introspect), so
        # the __aenter__/__aexit__ members are real but opaque to the checker.
        conn, process = await self._context_manager.__aenter__()

        self.conn = conn
        self.process = process
        self._log.info("spawned %s (pid=%s)", " ".join(self._command), process.pid)

        return conn

    async def kill(self) -> None:
        """Kill the subprocess and all its descendants."""
        if self._context_manager:
            try:
                # Exit the context manager manually to mirror the manual
                # __aenter__ in spawn().  Swallow errors — the process may
                # already be dead during shutdown.
                # pylint: disable=unnecessary-dunder-call,no-member
                await self._context_manager.__aexit__(None, None, None)
            except Exception:  # pylint: disable=broad-exception-caught
                self._log.debug(
                    "context manager exit failed during kill", exc_info=True
                )
            self._context_manager = None

        # Fallback: if the process is still alive, force-kill the tree
        if self.process and self.process.pid and self.process.returncode is None:
            pid = self.process.pid
            await asyncio.to_thread(_kill_process_tree, pid)

        self.conn = None
        self.process = None

    def is_alive(self) -> bool:
        """Return True if the subprocess is still running."""
        return self.process is not None and self.process.returncode is None
