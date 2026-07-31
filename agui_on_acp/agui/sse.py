"""SSE encoder for AG-UI events."""

import asyncio
import logging
from typing import Any, AsyncGenerator, Awaitable, Callable

from agui_on_acp.agui.events import AguiEvent, AguiEventType

logger = logging.getLogger(__name__)


def encode_sse_event(event: AguiEvent) -> str:
    """Serialize ``event`` to an SSE frame string (``event:`` + ``data:`` lines)."""
    json_str = event.model_dump_json(exclude_none=True)
    return f"event: {event.type.value}\ndata: {json_str}\n\n"


async def event_stream(
    queue: asyncio.Queue[AguiEvent],
    task_id: str,
    timeout: float = 30.0,
    on_cancel: Callable[[], Awaitable[Any]] | None = None,
) -> AsyncGenerator[str, None]:
    """Drain AG-UI events from ``queue`` as SSE.

    Terminates after ``RUN_FINISHED`` / ``RUN_ERROR`` (clean end-of-run).
    On ``CancelledError`` (client disconnect), calls ``on_cancel`` so the
    ACP turn is cancelled instead of orphaned. A normal interrupt-suspend
    stops via a clean ``RUN_FINISHED`` return — NOT a ``CancelledError`` —
    so interrupts are never mistaken for cancels.
    """
    logger.info("SSE stream started for task %s", task_id)

    while True:
        try:
            event: AguiEvent = await asyncio.wait_for(queue.get(), timeout=timeout)
            yield encode_sse_event(event)
            if event.type in (AguiEventType.RUN_FINISHED, AguiEventType.RUN_ERROR):
                logger.info(
                    "SSE stream ending for task %s (event=%s)",
                    task_id,
                    event.type.value,
                )
                return
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        except asyncio.CancelledError:
            logger.info("SSE stream cancelled (client disconnect) for task %s", task_id)
            if on_cancel is not None:
                # Broad catch: the cancel callback must not re-raise into the
                # ASGI layer or uvicorn will log a noisy traceback on a routine
                # disconnect. logger.exception captures the full trace.
                try:
                    await on_cancel()
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.exception("on_cancel callback failed for task %s", task_id)
            return
        except Exception:  # pylint: disable=broad-exception-caught
            # Broad catch: the SSE generator is the boundary between the event
            # queue and the ASGI response — any unhandled exception here would
            # propagate into Starlette and crash the connection without a clean
            # error frame. logger.exception captures the full trace.
            logger.exception("SSE stream error for task %s", task_id)
            return
