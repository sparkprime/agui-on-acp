"""Configuration loader for the ACP → AG-UI Bridge.

Reads bridge.config.json from the project root and exposes a typed
BridgeConfig object. Falls back to sensible defaults when the file
is missing, empty, or malformed.
"""

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_CAMEL_TO_SNAKE: dict[str, str] = {
    "projectName": "project_name",
    "displayTitle": "display_title",
    "description": "description",
    "agentCommand": "agent_command",
    "backendPort": "backend_port",
    "corsOrigins": "cors_origins",
    "dbDirectory": "db_directory",
}


class BridgeConfig(BaseModel):
    """Typed configuration for the bridge."""

    project_name: str = "acp-to-agui"
    display_title: str = "ACP → AG-UI Bridge"
    description: str = "Give any ACP-compatible coding agent a rich web UI"
    agent_command: list[str] = Field(default=["kiro-cli", "acp"])
    backend_port: int = 8000
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000"]
    )
    db_directory: str = ""

    @property
    def db_path(self) -> str:
        """Derive the SQLite database path from the project name."""
        dir_name = self.db_directory or f".{self.project_name}"
        return f"~/{dir_name}/tasks.db"


def _camel_to_snake(data: dict[str, Any]) -> dict[str, Any]:
    """Map camelCase JSON keys to their snake_case Python equivalents."""
    mapped: dict[str, Any] = {}
    for key, value in data.items():
        snake_key = _CAMEL_TO_SNAKE.get(key, key)
        mapped[snake_key] = value
    return mapped


def load_config(config_path: str = "bridge.config.json") -> BridgeConfig:
    """Load bridge config from a JSON file.

    Falls back to defaults when the file is missing or invalid.
    """
    path = Path(config_path)

    if not path.exists():
        logger.warning("Config '%s' not found — using defaults.", config_path)
        return BridgeConfig()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not read '%s': %s — using defaults.", config_path, exc)
        return BridgeConfig()

    if not raw_text.strip():
        logger.warning("Config '%s' is empty — using defaults.", config_path)
        return BridgeConfig()

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("Invalid JSON in '%s': %s — using defaults.", config_path, exc)
        return BridgeConfig()

    if not isinstance(raw_data, dict):
        logger.warning("Config '%s' is not a JSON object — using defaults.", config_path)
        return BridgeConfig()

    snake_data = _camel_to_snake(raw_data)

    try:
        return BridgeConfig(**snake_data)
    except Exception as exc:
        logger.warning("Invalid config values in '%s': %s — using defaults.", config_path, exc)
        return BridgeConfig()
