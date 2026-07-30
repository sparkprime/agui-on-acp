"""Entry point for running the bridge with `python -m agui_on_acp`.

Exposes the `agui-on-acp` console script as a Click CLI. Every field of
the configuration is available as a flag; there is no config file.

Example::

    agui-on-acp --agent-command "opencode acp" --port 8000

The CLI forwards the resolved flag values to the uvicorn worker via
individual ``AGUI_ON_ACP_*`` environment variables (see
:mod:`agui_on_acp.config`). Only flags explicitly passed on the command
line overwrite the environment; absent flags fall back to the env var or
the built-in default, so the bridge can also be configured purely
through the environment.
"""

from __future__ import annotations

import os

import click
import uvicorn

from agui_on_acp.config import (
    agent_command,
    backend_port,
    cors_origins,
    data_dir,
    env_var_name,
)


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--agent-command",
    "agent_command_flag",
    metavar="COMMAND",
    help=(
        "Command (and args) used to spawn the ACP agent, as a single "
        'shell-style string, e.g. `--agent-command "opencode acp"`. '
        f"Default: {' '.join(agent_command())}."
    ),
)
@click.option(
    "--port",
    "backend_port_flag",
    type=int,
    default=backend_port(),
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
    "cors_origins_flag",
    multiple=True,
    metavar="ORIGIN",
    help=(
        "Allowed CORS origin. Repeat for multiple origins. "
        f"Default: {', '.join(cors_origins())}."
    ),
)
@click.option(
    "--data-dir",
    "data_dir_flag",
    metavar="DIR",
    help=(
        "Base directory for the bridge's persistent state (per-session cwd "
        f"records). Default: {data_dir()}."
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
    agent_command_flag: str | None,
    backend_port_flag: int,
    host: str,
    cors_origins_flag: tuple[str, ...],
    data_dir_flag: str | None,
    reload: bool,
    log_level: str,
) -> None:
    """Run the AG-UI on ACP bridge server."""
    # Only flags the user actually passed overwrite the environment, so
    # that env-var-only configuration (or pre-set env vars) is respected.
    src = click.get_current_context().get_parameter_source

    def explicitly(name: str) -> bool:
        return src(name) in (
            click.core.ParameterSource.COMMANDLINE,
            click.core.ParameterSource.ENVIRONMENT,
        )

    if explicitly("agent_command_flag") and agent_command_flag:
        os.environ[env_var_name(agent_command)] = agent_command_flag
    if explicitly("backend_port_flag"):
        os.environ[env_var_name(backend_port)] = str(backend_port_flag)
    if explicitly("cors_origins_flag") and cors_origins_flag:
        os.environ[env_var_name(cors_origins)] = ",".join(cors_origins_flag)
    if explicitly("data_dir_flag") and data_dir_flag:
        os.environ[env_var_name(data_dir)] = data_dir_flag

    port = backend_port()
    click.echo(
        f"Starting AG-UI on ACP bridge on http://{host}:{port} "
        f"(agent: {' '.join(agent_command())})"
    )

    uvicorn.run(
        "agui_on_acp.main:app",
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )


if __name__ == "__main__":
    main()
