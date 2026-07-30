"""ACP → AG-UI Bridge — Give any ACP-compatible coding agent a rich web UI.

Translates Agent Communication Protocol (JSON-RPC 2.0 over stdio) into
AG-UI events (SSE) that any React frontend can consume.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

try:
    __version__ = _version(__name__.replace(".", "-"))
except PackageNotFoundError:  # not installed (e.g. running from source)
    __version__ = "0.0.0"
