"""TaskManager — orchestrates task lifecycle.

Manages active tasks: spawns agent via AgentRunner, initialises ACP,
holds per-task state, and coordinates run/approval flows.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.types.acp import PromptContent
from backend.bridge.acp_to_agui import AcpToAguiBridge
from backend.agent.acp_protocol import AcpProtocol
from backend.agent.runner import AgentRunner
from backend.policy.tool_policy import ToolPolicyEngine
from backend.tasks.store import TaskStore

logger = logging.getLogger(__name__)


@dataclass
class ActiveTask:
    task_id: str
    agent_session_id: str
    cwd: str
    runner: AgentRunner
    protocol: AcpProtocol
    event_queues: dict[str, asyncio.Queue] = field(default_factory=dict)
    current_run_id: str | None = None
    modes: list[dict[str, str]] | None = None
    models: list[dict[str, str]] | None = None
    current_mode_id: str | None = None
    pending_permissions: dict[str, int | str] = field(default_factory=dict)
    bridge: AcpToAguiBridge | None = None


class TaskManager:
    def __init__(self, store: TaskStore, agent_command: list[str] | None = None) -> None:
        self._store = store
        self._tasks: dict[str, ActiveTask] = {}
        self._agent_command = agent_command or ["kiro-cli", "acp"]

    async def create_task(
        self,
        task_id: str,
        cwd: str,
        title: str = "New Task",
        resume_session_id: str | None = None,
        mode: str | None = None,
        model: str | None = None,
        mcp_servers: dict[str, Any] | None = None,
    ) -> ActiveTask:
        runner = AgentRunner(task_id, command=self._agent_command)
        protocol = AcpProtocol(runner)

        await runner.spawn()
        await protocol.initialize()

        mcp_list = list(mcp_servers.values()) if mcp_servers else []
        modes: list[dict[str, str]] | None = None
        models: list[dict[str, str]] | None = None
        current_mode_id: str | None = None

        if resume_session_id:
            result = await protocol.session_load(resume_session_id, cwd, mcp_list)
            agent_session_id = result.get("sessionId", resume_session_id)
            if "modes" in result and result["modes"]:
                modes = [{"id": m["id"], "name": m["name"]} for m in result["modes"].get("availableModes", [])]
                current_mode_id = result["modes"].get("currentModeId")
            if "models" in result and result["models"]:
                models = [{"id": m.get("modelId", ""), "name": m.get("name", "")} for m in result["models"].get("availableModels", [])]
        else:
            result = await protocol.session_new(cwd, mcp_list)
            agent_session_id = result.sessionId
            if result.modes:
                modes = [{"id": m.id, "name": m.name} for m in result.modes.availableModes]
                current_mode_id = result.modes.currentModeId

        if mode and agent_session_id:
            try:
                await protocol.set_mode(agent_session_id, mode)
                current_mode_id = mode
            except Exception as exc:
                logger.warning("Failed to set mode %s: %s", mode, exc)
        if model and agent_session_id:
            try:
                await protocol.set_model(agent_session_id, model)
            except Exception as exc:
                logger.warning("Failed to set model %s: %s", model, exc)

        active = ActiveTask(
            task_id=task_id,
            agent_session_id=agent_session_id,
            cwd=cwd,
            runner=runner,
            protocol=protocol,
            modes=modes,
            models=models,
            current_mode_id=current_mode_id,
        )

        policy = ToolPolicyEngine(workspace_cwd=cwd)
        bridge = AcpToAguiBridge(task_id, policy)
        bridge.on_pending_permission = lambda call_id, rpc_id, params: (
            active.pending_permissions.__setitem__(call_id, rpc_id)
        )
        active.bridge = bridge

        runner.on_notification = bridge.handle_notification
        runner.on_request = bridge.handle_request
        runner.on_exit = lambda code: self._on_runner_exit(task_id, code)

        self._tasks[task_id] = active

        await self._store.create(
            task_id=task_id, agent_session_id=agent_session_id, cwd=cwd, title=title
        )
        logger.info("Task %s created (session=%s, cwd=%s)", task_id, agent_session_id, cwd)
        return active

    async def start_run(self, task_id: str, input_data: dict[str, Any], config: dict[str, Any] | None = None) -> str:
        active = self._get_active(task_id)
        run_id = str(uuid.uuid4())

        queue: asyncio.Queue = asyncio.Queue()
        active.event_queues[run_id] = queue
        active.current_run_id = run_id

        messages = input_data.get("messages", [])
        text = ""
        attachments: list[dict[str, Any]] = []
        if messages:
            last = messages[-1]
            text = last.get("content", "")
            attachments = last.get("attachments", [])

        content: list[PromptContent] = []
        if text:
            content.append(PromptContent(type="text", text=text))

        for att in attachments:
            att_type = att.get("type", "file")
            att_name = att.get("name", "unnamed")
            att_mime = att.get("mimeType", "application/octet-stream")
            att_data = att.get("data", "")
            if att_type == "image":
                content.append(PromptContent(type="image", data=att_data, mimeType=att_mime))
            else:
                try:
                    import base64
                    decoded = base64.b64decode(att_data).decode("utf-8", errors="replace")
                    content.append(PromptContent(type="text", text=f"[File: {att_name}]\n```\n{decoded}\n```"))
                except Exception:
                    content.append(PromptContent(type="text", text=f"[File: {att_name} — could not decode]"))

        if not content:
            content.append(PromptContent(type="text", text=""))

        if active.bridge is not None:
            active.bridge.start_run(run_id, queue)

        await self._store.update(task_id, status="running")
        asyncio.create_task(self._run_prompt(active, run_id, content))
        return run_id

    async def _run_prompt(self, active: ActiveTask, run_id: str, content: list[PromptContent]) -> None:
        from backend.agui.events import RunErrorEvent

        queue = active.event_queues.get(run_id)
        if queue is None:
            return
        try:
            await active.protocol.session_prompt(active.agent_session_id, content)
            if active.bridge and active.bridge._run_id is not None:
                active.bridge.finish_run()
        except Exception as exc:
            logger.error("Run %s failed: %s", run_id, exc)
            if active.bridge:
                active.bridge.error_run(str(exc))
            else:
                await queue.put(RunErrorEvent(runId=run_id, taskId=active.task_id, message=str(exc)))
        finally:
            await self._store.update(active.task_id, status="idle")

    def get_event_queue(self, task_id: str, run_id: str) -> asyncio.Queue | None:
        active = self._tasks.get(task_id)
        if active is None:
            return None
        return active.event_queues.get(run_id)

    async def approve(self, task_id: str, call_id: str, approved: bool, option_id: str | None = None) -> None:
        active = self._get_active(task_id)
        rpc_id = active.pending_permissions.pop(call_id, None)
        if rpc_id is None:
            logger.warning("No pending permission for call_id=%s", call_id)
            return
        if approved:
            outcome = {"outcome": "selected", "optionId": option_id or "allow_once"}
        else:
            outcome = {"outcome": "cancelled"}
        active.protocol.respond(rpc_id, {"outcome": outcome})
        if active.bridge:
            active.bridge.on_approval_resolved(call_id, approved)

    async def cancel_run(self, task_id: str) -> None:
        active = self._get_active(task_id)
        active.protocol.session_cancel(active.agent_session_id)
        await self._store.update(task_id, status="idle")

    async def set_mode(self, task_id: str, mode_id: str) -> Any:
        active = self._get_active(task_id)
        result = await active.protocol.set_mode(active.agent_session_id, mode_id)
        active.current_mode_id = mode_id
        return result

    async def set_model(self, task_id: str, model_id: str) -> None:
        active = self._get_active(task_id)
        await active.protocol.set_model(active.agent_session_id, model_id)

    async def execute_command(self, task_id: str, command: str, args: dict[str, Any] | None = None) -> None:
        active = self._get_active(task_id)
        args_str = args.get("args", "") if args else ""
        await active.protocol.execute_command(active.agent_session_id, command, args_str)

    async def stop(self, task_id: str) -> bool:
        active = self._tasks.pop(task_id, None)
        if active:
            await active.runner.kill()
            return True
        return False

    async def destroy(self, task_id: str) -> None:
        active = self._tasks.pop(task_id, None)
        if active:
            await active.runner.kill()

    async def shutdown(self) -> None:
        await asyncio.gather(
            *(self.destroy(tid) for tid in list(self._tasks.keys())),
            return_exceptions=True,
        )

    def _get_active(self, task_id: str) -> ActiveTask:
        active = self._tasks.get(task_id)
        if active is None:
            raise KeyError(f"No active task: {task_id}")
        return active

    def _on_runner_exit(self, task_id: str, exit_code: int | None) -> None:
        from backend.agui.events import RunErrorEvent

        active = self._tasks.pop(task_id, None)
        if active is not None:
            error_msg = f"Agent process exited unexpectedly (code={exit_code}). Please create a new task."
            for run_id, queue in active.event_queues.items():
                try:
                    queue.put_nowait(RunErrorEvent(runId=run_id, taskId=task_id, message=error_msg))
                except Exception:
                    pass
        asyncio.ensure_future(self._store.update(task_id, status="error"))
