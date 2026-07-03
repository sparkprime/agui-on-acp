"""In-process asyncio stream pair — a fake subprocess transport.

The bridge normally spawns the ACP agent as a child process
(``acp.spawn_agent_process``) and talks newline-delimited JSON-RPC over the
subprocess's stdin/stdout. For tests we replace only that subprocess with
two ``asyncio.StreamReader``/``StreamWriter`` pairs connected by in-memory
pipes: the client side writes to a pipe the agent reads from, and vice
versa. Everything above the transport — ``ClientSideConnection``,
``AgentSideConnection``, the JSON-RPC framing, the bridge's ``acp.Client``
callback dispatch — runs unmodified.

This is the maximum amount of code we can keep under test without spawning
a real OS process. The only code NOT exercised here is
``spawn_agent_process`` / ``asyncio.create_subprocess_exec`` and the
``AgentRunner`` subprocess-kill tree, which is OS plumbing, not protocol
translation.
"""

from __future__ import annotations

import asyncio
from asyncio import transports as aio_transports
from typing import Any

__all__ = ["make_transport_pair", "TransportPair"]


class _PipeTransport(aio_transports.WriteTransport):
    """A write transport that feeds its bytes into a paired StreamReader.

    Mirrors what ``asyncio.StreamWriter`` expects from its transport: a
    non-blocking ``write`` plus ``write_eof``/``close``/``is_closing`` and a
    no-op ``drain`` (the StreamWriter's ``drain`` returns immediately when
    the transport never pauses writing).
    """

    def __init__(self, peer_reader: asyncio.StreamReader) -> None:
        self._peer = peer_reader
        self._closing = False

    def write(self, data: bytes) -> None:  # type: ignore[override]
        if self._closing:
            return
        self._peer.feed_data(data)

    def write_eof(self) -> None:  # type: ignore[override]
        if not self._peer.at_eof():
            self._peer.feed_eof()

    def can_write_eof(self) -> bool:  # type: ignore[override]
        return True

    def close(self) -> None:  # type: ignore[override]
        self._closing = True
        if not self._peer.at_eof():
            self._peer.feed_eof()

    def is_closing(self) -> bool:  # type: ignore[override]
        return self._closing

    def abort(self) -> None:  # type: ignore[override]
        self.close()

    def get_extra_info(self, name: str, default: Any = None) -> Any:  # type: ignore[override]
        return default


class TransportPair:
    """The four endpoints of an in-memory bidirectional stream pair.

    ``client_reader``/``client_writer`` are handed to the bridge's
    ``ClientSideConnection``; ``agent_reader``/``agent_writer`` are handed
    to the fake agent's ``AgentSideConnection``. Writing on one side feeds
    the reader on the other.
    """

    __slots__ = (
        "client_reader",
        "client_writer",
        "agent_reader",
        "agent_writer",
    )

    def __init__(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        agent_reader: asyncio.StreamReader,
        agent_writer: asyncio.StreamWriter,
    ) -> None:
        self.client_reader = client_reader
        self.client_writer = client_writer
        self.agent_reader = agent_reader
        self.agent_writer = agent_writer


def make_transport_pair() -> TransportPair:
    """Create a connected pair of asyncio StreamReader/StreamWriter endpoints."""
    loop = asyncio.get_event_loop()
    client_reader: asyncio.StreamReader = asyncio.StreamReader()
    agent_reader: asyncio.StreamReader = asyncio.StreamReader()

    client_writer = asyncio.StreamWriter(
        _PipeTransport(agent_reader),
        asyncio.StreamReaderProtocol(client_reader),
        client_reader,
        loop,
    )
    agent_writer = asyncio.StreamWriter(
        _PipeTransport(client_reader),
        asyncio.StreamReaderProtocol(agent_reader),
        agent_reader,
        loop,
    )
    return TransportPair(client_reader, client_writer, agent_reader, agent_writer)
