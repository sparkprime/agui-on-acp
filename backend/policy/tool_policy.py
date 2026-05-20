"""ToolPolicyEngine — decides whether a tool call requires human approval.

The policy currently defers to the agent's own `requiresApproval` flag.
A richer rule-based engine (file-system scope, command allow-lists,
MCP server trust) can be layered on later.
"""

import logging
from typing import Any

from backend.policy.types import PolicyDecision

logger = logging.getLogger(__name__)


class ToolPolicyEngine:
    """Rule-based policy engine for tool-call approval decisions."""

    def __init__(self, workspace_cwd: str | None = None) -> None:
        self._cwd = workspace_cwd

    def evaluate(self, tool_name: str, args: dict[str, Any], *, kiro_requires: bool = False) -> PolicyDecision:
        """Evaluate whether a tool call needs approval.

        Args:
            tool_name: Name of the tool being invoked.
            args: Tool arguments (may contain file paths, commands, etc.).
            kiro_requires: Whether the agent flagged this call as requiring
                approval (``requiresApproval`` in the ACP notification).

        Returns:
            A ``PolicyDecision`` indicating whether approval is needed.
        """
        if kiro_requires:
            category = self._categorise(tool_name)
            return PolicyDecision(
                requires_approval=True,
                reason=f"Agent flagged {tool_name} as requiring approval",
                category=category,
            )

        return PolicyDecision(requires_approval=False)

    # ── Internal helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _categorise(tool_name: str) -> str:
        name = tool_name.lower()
        if any(kw in name for kw in ("file", "write", "read", "delete", "mkdir", "fs")):
            return "filesystem"
        if any(kw in name for kw in ("exec", "run", "command", "shell", "bash", "terminal")):
            return "command"
        if any(kw in name for kw in ("http", "fetch", "curl", "request", "api")):
            return "network"
        if "mcp" in name:
            return "mcp"
        return "other"
