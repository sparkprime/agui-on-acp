"""Configuration loader for the ACP → AG-UI Bridge.

Resolution order (first wins):
  1. ``AGUI_ON_ACP_CONFIG_JSON`` env var — a JSON blob built by the Click
     CLI in ``__main__.py`` and forwarded to the uvicorn worker.
  2. ``AGUI_ON_ACP_CONFIG_PATH`` env var / ``config_path`` argument — a
     path to a ``bridge.config.json`` file on disk.
  3. Built-in ``BridgeConfig`` defaults.

Falls back to sensible defaults when a source is missing, empty, or
malformed.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_ENV_CONFIG_JSON = "AGUI_ON_ACP_CONFIG_JSON"
_ENV_CONFIG_PATH = "AGUI_ON_ACP_CONFIG_PATH"

_CAMEL_TO_SNAKE: dict[str, str] = {
    "projectName": "project_name",
    "displayTitle": "display_title",
    "description": "description",
    "agentCommand": "agent_command",
    "backendPort": "backend_port",
    "corsOrigins": "cors_origins",
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


def _camel_to_snake(data: dict[str, Any]) -> dict[str, Any]:
    """Map camelCase JSON keys to their snake_case Python equivalents."""
    mapped: dict[str, Any] = {}
    for key, value in data.items():
        snake_key = _CAMEL_TO_SNAKE.get(key, key)
        mapped[snake_key] = value
    return mapped


def _build_config(data: dict[str, Any], source: str) -> BridgeConfig:
    """Construct a BridgeConfig from a raw dict, mapping camelCase keys."""
    snake_data = _camel_to_snake(data)
    try:
        return BridgeConfig(**snake_data)
    except Exception as exc:
        logger.warning(
            "Invalid config values in %s: %s — using defaults.", source, exc
        )
        return BridgeConfig()


def load_config(config_path: str = "bridge.config.json") -> BridgeConfig:
    """Load bridge config.

    Resolution order (first wins):
      1. ``AGUI_ON_ACP_CONFIG_JSON`` env var (JSON blob from the CLI).
      2. The file at ``AGUI_ON_ACP_CONFIG_PATH`` env var, if set.
      3. The file at ``config_path`` (default ``bridge.config.json``).
      4. Built-in defaults.

    Falls back to defaults when a source is missing or invalid.
    """
    # 1. Env-var JSON blob (set by the Click CLI when flags are passed).
    env_json = os.environ.get(_ENV_CONFIG_JSON)
    if env_json:
        try:
            raw_data = json.loads(env_json)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Invalid JSON in %s: %s — using defaults.", _ENV_CONFIG_JSON, exc
            )
            return BridgeConfig()
        if not isinstance(raw_data, dict):
            logger.warning(
                "%s is not a JSON object — using defaults.", _ENV_CONFIG_JSON
            )
            return BridgeConfig()
        return _build_config(cast(dict[str, Any], raw_data), _ENV_CONFIG_JSON)

    # 2/3. File path — prefer the env-var path, then the explicit argument.
    resolved_path = os.environ.get(_ENV_CONFIG_PATH, config_path)
    path = Path(resolved_path)

    if not path.exists():
        logger.warning("Config '%s' not found — using defaults.", resolved_path)
        return BridgeConfig()

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "Could not read '%s': %s — using defaults.", resolved_path, exc
        )
        return BridgeConfig()

    if not raw_text.strip():
        logger.warning("Config '%s' is empty — using defaults.", resolved_path)
        return BridgeConfig()

    try:
        raw_data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning(
            "Invalid JSON in '%s': %s — using defaults.", resolved_path, exc
        )
        return BridgeConfig()

    if not isinstance(raw_data, dict):
        logger.warning(
            "Config '%s' is not a JSON object — using defaults.", resolved_path
        )
        return BridgeConfig()

    return _build_config(cast(dict[str, Any], raw_data), str(path))
