"""Configuration for the ACP → AG-UI Bridge.

Every option is read from an environment variable carrying the
``AGUI_ON_ACP_`` prefix. Each option has a dedicated accessor function
(e.g. :func:`agent_command`) that parses the corresponding variable on
every call, so the bridge can be configured purely through the
environment without a config file or a CLI.

The env-var name for an accessor is derived from its function name
(uppercased and prefixed with ``AGUI_ON_ACP_``), so there is a single
source of truth: rename the function and the env var follows. The
supported variable set is discovered by introspecting this module for
functions marked with :func:`_config_accessor` — no hand-maintained
list.

``list[str]`` options are comma-separated. Use :func:`validate_env_vars`
to surface typos in unknown ``AGUI_ON_ACP_`` variables and
:func:`log_the_config` to dump the effective configuration at startup.
"""

import inspect
import logging
import os
import shlex
import sys
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

_PREFIX = "AGUI_ON_ACP_"

F = TypeVar("F", bound=Callable[..., Any])


def _config_accessor(fn: F) -> F:
    """Mark ``fn`` as a config accessor so :func:`validate_env_vars` and
    :func:`log_the_config` can discover it by introspecting this module.

    Returns ``fn`` unchanged so that the caller-name lookup performed by
    the parsing helpers (via ``inspect.currentframe``) sees the real
    function name rather than a wrapper's.
    """
    setattr(fn, "_is_config_accessor", True)
    return fn


def _caller_name() -> str:
    """Return the name of the function that called *our caller*.

    The parsing helpers below (``_env_var`` / ``_env_var_int`` /
    ``_env_var_list``) call this, so our caller is the helper and the
    helper's caller is the config accessor — whose name maps 1:1 to the
    env-var name. Grabbing the frame in the helper itself (rather than
    here) would only reach the helper; going one more level up reaches
    the accessor.
    """
    frame = inspect.currentframe()
    try:
        helper = frame.f_back if frame is not None else None
        accessor = helper.f_back if helper is not None else None
        return accessor.f_code.co_name if accessor is not None else ""
    finally:
        # Break the reference cycle that frame objects create.
        del frame


def env_var_name(accessor: Callable[..., Any]) -> str:
    """Return the ``AGUI_ON_ACP_*`` env-var name backing ``accessor``.

    Convenience for writers (e.g. the CLI in ``__main__.py``) that need
    to set the same variable an accessor reads, without duplicating the
    literal.
    """
    return _PREFIX + accessor.__name__.upper()


def _accessors() -> list[Callable[..., Any]]:
    """Return every config accessor in this module, in definition order."""
    module = sys.modules[__name__]
    found: list[Callable[..., Any]] = []
    for obj in vars(module).values():
        if callable(obj) and getattr(obj, "_is_config_accessor", False):
            found.append(obj)
    return found


def _recognised_env_vars() -> frozenset[str]:
    """Return the set of env-var names backed by an accessor in this module."""
    return frozenset(env_var_name(fn) for fn in _accessors())


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _env_var(default: str) -> str:
    """Return the caller's env var as a raw string, or ``default`` if unset."""
    name = _PREFIX + _caller_name().upper()
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def _env_var_int(default: int) -> int:
    """Return the caller's env var parsed as ``int`` or ``default`` on failure."""
    name = _PREFIX + _caller_name().upper()
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a valid int — using default %d", name, raw, default
        )
        return default


def _env_var_list(default: list[str]) -> list[str]:
    """Return the caller's env var split with :func:`shlex.split`, or
    ``default`` if unset.

    ``shlex`` (not comma-split) is used so that a single env-var string
    like ``"opencode acp"`` parses into ``["opencode", "acp"]`` the same
    way a shell would, including quoting for args that contain spaces.
    """
    name = _PREFIX + _caller_name().upper()
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return list(default)
    return shlex.split(raw)


# --------------------------------------------------------------------------- #
# Config accessors
# --------------------------------------------------------------------------- #
@_config_accessor
def agent_command() -> list[str]:
    """Command (and args) used to spawn the ACP agent, shell-style.

    Env var: ``AGUI_ON_ACP_AGENT_COMMAND`` — a single string parsed with
    :func:`shlex.split`, e.g. ``"opencode acp"`` or ``"npx -y my-agent"``.
    """
    return _env_var_list(["opencode", "acp"])


@_config_accessor
def backend_port() -> int:
    """Port the bridge's HTTP server listens on.

    Env var: ``AGUI_ON_ACP_BACKEND_PORT``
    """
    return _env_var_int(8000)


@_config_accessor
def cors_origins() -> list[str]:
    """Allowed CORS origins, comma-separated.

    Env var: ``AGUI_ON_ACP_CORS_ORIGINS``
    """
    return _env_var_list(
        ["http://localhost:5173", "http://localhost:3000", "http://localhost:4200"]
    )


@_config_accessor
def allowed_cwd_prefixes() -> list[str]:
    """Absolute path prefixes a client-supplied ``cwd`` must fall under.

    Empty (default) means: only the bridge's own ``os.getcwd()`` is allowed
    — NOT client-controlled arbitrary paths, unlike the old implicit ``"."``
    fallback. Env var: ``AGUI_ON_ACP_ALLOWED_CWD_PREFIXES`` (shlex-split,
    so paths containing spaces can be quoted).
    """
    return _env_var_list([])


@_config_accessor
def idle_ttl_seconds() -> float:
    """Seconds of inactivity after which an idle ``ActiveSession`` is reaped.

    Env var: ``AGUI_ON_ACP_IDLE_TTL_SECONDS``
    """
    return float(_env_var("1800"))


def is_cwd_allowed(cwd: str) -> bool:
    """Return True if ``cwd`` falls under an allowlisted prefix.

    When no prefixes are configured, the bridge's own ``os.getcwd()`` is
    the only allowed root — closing the gap flagged in PLAN3 item 8 where a
    browser could pass an arbitrary ``cwd`` (defaulting to ``"."``).
    """
    if not cwd:
        return False
    real = os.path.realpath(cwd)
    prefixes = allowed_cwd_prefixes() or [os.getcwd()]
    for p in prefixes:
        rp = os.path.realpath(p)
        if real == rp or real.startswith(rp.rstrip(os.sep) + os.sep):
            return True
    return False


# --------------------------------------------------------------------------- #
# Diagnostics
# --------------------------------------------------------------------------- #
def validate_env_vars() -> None:
    """Warn about any ``AGUI_ON_ACP_`` env vars this module does not recognise.

    Catches typos (e.g. ``AGUI_ON_ACP_PORT`` instead of
    ``AGUI_ON_ACP_BACKEND_PORT``) and leftover variables from older
    versions. Unrecognised variables are ignored at runtime; this only
    emits a warning. The recognised set is discovered by introspecting
    this module for :func:`_config_accessor`-marked functions.
    """
    recognised = _recognised_env_vars()
    for name in sorted(os.environ):
        if name.startswith(_PREFIX) and name not in recognised:
            logger.warning(
                "Unrecognised env var %s is not a known AGUI_ON_ACP option — "
                "ignoring. Check for typos; recognised options: %s",
                name,
                ", ".join(sorted(recognised)),
            )


def log_the_config() -> None:
    """Log every configuration option with its current effective value."""
    logger.info("AG-UI on ACP configuration:")
    for accessor in _accessors():
        logger.info("  %s = %r", env_var_name(accessor), accessor())
