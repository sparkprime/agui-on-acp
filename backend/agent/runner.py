"""AgentRunner — manages a single ACP agent subprocess.

Spawns a configurable ACP-compatible agent binary, communicates via
JSON-RPC 2.0 over newline-delimited JSON on stdin/stdout.
"""

import asyncio
import json
import logging
import os
import signal
import shutil
import subprocess
from typing import Any, Callable

from backend.types.acp import JsonRpcNotification, JsonRpcRequest

logger = logging.getLogger(__name__)


def _kill_process_tree(root_pid: int) -> None:
    """Kill all descendants of root_pid, then root_pid itself."""
    ps_path = shutil.which("ps") or "/bin/ps"
    try:
        out = subprocess.check_output([ps_path, "-eo", "pid,ppid"], text=True)
    except Exception:
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


class AgentRunner:
    """Manages a single ACP agent subprocess.

    Attributes:
        task_id: Identifier of the owning task.
        on_notification: Called when the agent sends a notification.
        on_request: Called when the agent sends a request requiring a response.
        on_exit: Called when the subprocess exits.
    """

    def __init__(self, task_id: str, command: list[str]) -> None:
        self.task_id = task_id
        self._command = command
        self._process: asyncio.subprocess.Process | None = None
        self._next_id: int = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._log = logging.LoggerAdapter(logger, {"task_id": task_id})

        self.on_notification: Callable[[str, dict[str, Any]], None] | None = None
        self.on_request: Callable[[str, dict[str, Any], int | str], None] | None = None
        self.on_exit: Callable[[int | None], None] | None = None

    async def spawn(self, env: dict[str, str] | None = None, debug: bool = False) -> None:
        """Spawn the ACP agent as a child process."""
        proc_env = os.environ.copy()
        if debug:
            proc_env["AGENT_LOG_LEVEL"] = "debug"
            proc_env["AGENT_LOG_FILE"] = f"/tmp/acp-agent-{self.task_id}.log"
        if env:
            proc_env.update(env)

        buf_limit = 16 * 1024 * 1024  # 16 MB

        self._process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
            limit=buf_limit,
        )
        self._log.info("Spawned agent %s PID=%s", self._command, self._process.pid)

        self._reader_task = asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

    async def kill(self) -> None:
        """Kill the subprocess and all its descendants."""
        if self._process and self._process.pid:
            pid = self._process.pid
            self._process = None
            await asyncio.to_thread(_kill_process_tree, pid)
        if self._reader_task:
            self._reader_task.cancel()
            self._reader_task = None
        self._reject_all(RuntimeError("Process killed"))

    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        """Send a JSON-RPC request and wait for the response."""
        proc = self._ensure_process()
        request_id = self._next_id
        self._next_id += 1

        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        rpc = JsonRpcRequest(id=request_id, method=method, params=params)
        msg = rpc.model_dump_json(by_alias=True, exclude_none=True)
        proc.stdin.write((msg + "\n").encode())
        await proc.stdin.drain()
        self._log.debug("→ request %d: %s", request_id, method)

        try:
            return await future
        except Exception as exc:
            self._log.error("Request %d (%s) failed: %s", request_id, method, exc)
            raise

    def notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (fire-and-forget)."""
        proc = self._ensure_process()
        notification = JsonRpcNotification(method=method, params=params)
        msg = notification.model_dump_json(by_alias=True, exclude_none=True)
        proc.stdin.write((msg + "\n").encode())
        self._log.debug("→ notify: %s", method)

    def respond(self, request_id: int | str, result: Any) -> None:
        """Respond to a request initiated by the agent."""
        proc = self._ensure_process()
        response_dict = {"jsonrpc": "2.0", "id": request_id, "result": result}
        msg = json.dumps(response_dict)
        proc.stdin.write((msg + "\n").encode())
        self._log.debug("→ respond id=%s", request_id)

    def _ensure_process(self) -> asyncio.subprocess.Process:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError(f"[task={self.task_id}] Agent process not running")
        return self._process

    async def _read_stdout(self) -> None:
        assert self._process and self._process.stdout
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                line_str = line.decode().strip()
                if not line_str:
                    continue
                self._log.info("[stdout] %s", line_str[:250])
                self._handle_line(line_str)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("Error reading stdout: %s", exc)
        finally:
            exit_code = self._process.returncode if self._process else None
            self._reject_all(RuntimeError(f"Process exited (code={exit_code})"))
            if self.on_exit:
                self.on_exit(exit_code)

    async def _read_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                msg = line.decode().strip()
                if msg:
                    self._log.warning("[stderr] %s", msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log.error("Error reading stderr: %s", exc)

    def _handle_line(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._log.warning("Invalid JSON: %s", line[:120])
            return

        msg_id = msg.get("id")
        method = msg.get("method")

        if msg_id is not None and method:
            self._log.debug("← agent request: %s (id=%s)", method, msg_id)
            if self.on_request:
                self.on_request(method, msg.get("params", {}), msg_id)
        elif msg_id is not None:
            future = self._pending.pop(msg_id, None)
            if future is None:
                self._log.warning("Response for unknown id=%s", msg_id)
                return
            error = msg.get("error")
            if error:
                err_msg = (
                    f"{error.get('message', 'Unknown error')} "
                    f"(code={error.get('code')}, data={error.get('data')})"
                )
                future.set_exception(RuntimeError(err_msg))
            else:
                future.set_result(msg.get("result"))
        elif method:
            self._log.info("← notification: %s", method)
            if self.on_notification:
                self.on_notification(method, msg.get("params", {}))
        else:
            self._log.warning("Unrecognised message: %s", line[:120])

    def _reject_all(self, error: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
