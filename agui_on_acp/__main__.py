"""Entry point for running the bridge with `python -m agui_on_acp`.

Exposes the `agui-on-acp` console script as a Click CLI. Every field of
:class:`BridgeConfig` is available as a flag, so the JSON config file is
optional. Flags override the file (and the file is still honoured when no
matching flag is given) — see :mod:`agui_on_acp.config` for the full
resolution order.

Example::

    agui-on-acp --agent-command opencode --agent-command acp --port 8000

The CLI serialises the resolved config to JSON and forwards it to the
uvicorn worker via the ``AGUI_ON_ACP_CONFIG_JSON`` env var, so the
FastAPI app (imported fresh by uvicorn, including under ``--reload``)
picks up the flag values without us having to monkeypatch the module.
"""

from __future__ import annotations

import os
from typing import Any

import click
import uvicorn

from agui_on_acp.config import BridgeConfig, load_config

# Default agent command used by the bridge. Surfaced as a module constant
# so the Click default mirrors :class:`BridgeConfig` without duplicating
# the literal.
_DEFAULT_AGENT_COMMAND: list[str] = ["kiro-cli", "acp"]
_DEFAULT_CORS_ORIGINS: list[str] = ["http://localhost:5173", "http://localhost:3000"]


def _agent_command_callback(
    ctx: click.Context, param: click.Parameter, value: tuple[str, ...]
) -> list[str] | None:
    """Accept ``--agent-command`` repeated, or a single comma-separated list."""
    if not value:
        return None
    expanded: list[str] = []
    for piece in value:
        expanded.extend(p.strip() for p in piece.split(",") if p.strip())
    return expanded


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--agent-command",
    "agent_command",
    multiple=True,
    metavar="TOKEN",
    callback=_agent_command_callback,
    help=(
        "Command (and args) used to spawn the ACP agent. Repeat the flag "
        "for multi-token commands, e.g. `--agent-command opencode "
        "--agent-command acp`. Tokens may also be comma-separated. "
        f"Default: {' '.join(_DEFAULT_AGENT_COMMAND)}."
    ),
)
@click.option(
    "--port",
    "backend_port",
    type=int,
    default=BridgeConfig.model_fields["backend_port"].default,
    show_default=True,
    help="Port the bridge's HTTP server listens on.",
)
@click.option(
    "--host",
    default="0.0.0.0",
    show_default=True,
    help="Host the bridge's HTTP server binds to.",
)
@click.option(
    "--cors-origin",
    "cors_origins",
    multiple=True,
    metavar="ORIGIN",
    help=(
        "Allowed CORS origin. Repeat for multiple origins. "
        f"Default: {', '.join(_DEFAULT_CORS_ORIGINS)}."
    ),
)
@click.option(
    "--project-name",
    default=BridgeConfig.model_fields["project_name"].default,
    show_default=True,
    help="Internal project identifier (used in /health responses).",
)
@click.option(
    "--display-title",
    "display_title",
    default=BridgeConfig.model_fields["display_title"].default,
    show_default=True,
    help="Title shown in the FastAPI/OpenAPI docs.",
)
@click.option(
    "--description",
    "description",
    default=BridgeConfig.model_fields["description"].default,
    show_default=True,
    help="Description shown in the OpenAPI docs.",
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(dir_okay=False),
    envvar="AGUI_ON_ACP_CONFIG_PATH",
    default="bridge.config.json",
    show_default=True,
    help=(
        "Path to a bridge.config.json file. Values for fields not given on "
        "the command line are read from this file. Set "
        "AGUI_ON_ACP_CONFIG_PATH to override programmatically."
    ),
)
@click.option(
    "--reload/--no-reload",
    default=True,
    show_default=True,
    help="Restart the worker on source changes (uvicorn --reload).",
)
@click.option(
    "--log-level",
    type=click.Choice(["critical", "error", "warning", "info", "debug", "trace"]),
    default="info",
    show_default=True,
    help="Uvicorn log level.",
)
def main(
    agent_command: list[str] | None,
    backend_port: int,
    host: str,
    cors_origins: tuple[str, ...],
    project_name: str,
    display_title: str,
    description: str,
    config_path: str,
    reload: bool,
    log_level: str,
) -> None:
    """Run the AG-UI on ACP bridge server.

    All configuration can be supplied via flags; a bridge.config.json file
    is optional. Explicitly-passed flags take precedence over the file.
    """
    base = load_config(config_path)
    merged: dict[str, Any] = base.model_dump()

    src = click.get_current_context().get_parameter_source

    def explicitly(name: str) -> bool:
        return src(name) in (
            click.core.ParameterSource.COMMANDLINE,
            click.core.ParameterSource.ENVIRONMENT,
        )

    if explicitly("agent_command") and agent_command is not None:
        merged["agent_command"] = agent_command
    if explicitly("backend_port"):
        merged["backend_port"] = backend_port
    if explicitly("cors_origin") and cors_origins:
        merged["cors_origins"] = list(cors_origins)
    if explicitly("project_name"):
        merged["project_name"] = project_name
    if explicitly("display_title"):
        merged["display_title"] = display_title
    if explicitly("description"):
        merged["description"] = description

    config = BridgeConfig(**merged)

    # Forward the resolved config to the uvicorn worker via env var. The
    # worker imports `agui_on_acp.main` fresh, where `load_config()` will
    # read AGUI_ON_ACP_CONFIG_JSON before falling back to the file.
    os.environ[_ENV_CONFIG_JSON_NAME] = config.model_dump_json()
    os.environ.pop(_ENV_CONFIG_PATH_NAME, None)

    click.echo(
        f"Starting AG-UI on ACP bridge on http://{host}:{config.backend_port} "
        f"(agent: {' '.join(config.agent_command)})"
    )

    uvicorn.run(
        "agui_on_acp.main:app",
        host=host,
        port=config.backend_port,
        reload=reload,
        log_level=log_level,
    )


# Names of the env vars consumed by config.load_config(). Kept here so the
# CLI is the only writer and config.py is the only reader; duplicating the
# literals avoids a circular import (config.py must not import from
# __main__.py).
_ENV_CONFIG_JSON_NAME = "AGUI_ON_ACP_CONFIG_JSON"
_ENV_CONFIG_PATH_NAME = "AGUI_ON_ACP_CONFIG_PATH"


if __name__ == "__main__":
    main()
