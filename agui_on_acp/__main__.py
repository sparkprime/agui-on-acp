"""Entry point for running the bridge with `python -m agui_on_acp`.

Exposes the `agui-on-acp` console script as a Click CLI. Every field of
the configuration is available as a flag; there is no config file.

Example::

    agui-on-acp run --agent-command "opencode acp" --port 8000

    agui-on-acp example-log-config

The CLI forwards the resolved flag values to the uvicorn worker via
individual ``AGUI_ON_ACP_*`` environment variables (see
:mod:`agui_on_acp.config`). Only flags explicitly passed on the command
line overwrite the environment; absent flags fall back to the env var or
the built-in default, so the bridge can also be configured purely
through the environment.
"""

import json
import os
import tempfile
from typing import Any

import click
import uvicorn

from agui_on_acp.config import (
    agent_command,
    backend_port,
    cors_origins,
    data_dir,
    env_var_name,
)

_DEFAULT_LOG_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s - %(name)s:%(lineno)d [%(levelname)s] %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S %z",
        },
    },
    "handlers": {
        "Console": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stdout",
            "level": "DEBUG",
        }
    },
    "root": {"level": "INFO", "handlers": ["Console"]},
    "loggers": {
        "httpx": {"level": "WARNING"},
    },
}


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
)
def main() -> None:
    """AG-UI on ACP bridge command-line interface."""


@main.command("example-log-config")
def example_log_config() -> None:
    """Print the default Python logging dictConfig as JSON (indent=2)."""
    click.echo(json.dumps(_DEFAULT_LOG_CONFIG, indent=2))


@main.command("run")
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
@click.option(
    "--log-config-file",
    "log_config_file",
    metavar="PATH",
    help=(
        "Path to a Python logging dictConfig JSON file passed to uvicorn as "
        "`log_config`. If omitted, the built-in default config is written to "
        "a temporary file and used instead."
    ),
)
def run(
    agent_command_flag: str | None,
    backend_port_flag: int,
    host: str,
    cors_origins_flag: tuple[str, ...],
    data_dir_flag: str | None,
    reload: bool,
    log_level: str,
    log_config_file: str | None,
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

    # When no log config file is supplied, materialise the default config
    # to a temporary JSON file so it can be passed to uvicorn's `log_config`
    # (a file path is required for reload mode, where the worker is spawned
    # in a separate process).
    owns_temp_file = False
    if log_config_file is None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".json",
            delete=False,
        ) as tmp:
            json.dump(_DEFAULT_LOG_CONFIG, tmp)
        log_config_file = tmp.name
        owns_temp_file = True

    try:
        uvicorn.run(
            "agui_on_acp.main:app",
            host=host,
            port=port,
            reload=reload,
            log_level=log_level,
            log_config=log_config_file,
        )
    finally:
        if owns_temp_file:
            try:
                os.unlink(log_config_file)
            except OSError:
                pass


if __name__ == "__main__":
    # pylint: disable=no-value-for-parameter
    # Click injects the parameters from the command line; the bare ``main()``
    # call is the standard Click entry-point pattern.
    main()
