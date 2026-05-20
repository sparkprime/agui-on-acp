"""SSE encoder for AG-UI events."""

import asyncio
import logging
from typing import AsyncGenerator

from backend.agui.events import AguiEventType, BaseAguiEvent

logger = logging.getLogger(__name__)


def encode_sse_event(event: BaseAguiEvent) -> str:
    json_str = event.model_dump_json(exclude_none=True)
    return f"event: {event.type.value}\ndata: {json_str}\n\n"


async def event_stream(
    queue: asyncio.Queue, task_id: str, timeout: float = 30.0
) -> AsyncGenerator[str, None]:
    logger.info("SSE stream started for task %s", task_id)

    while True:
        try:
            event: BaseAguiEvent = await asyncio.wait_for(queue.get(), timeout=timeout)
            yield encode_sse_event(event)
            if event.type in (AguiEventType.RUN_FINISHED, AguiEventType.RUN_ERROR):
                logger.info("SSE stream ending for task %s (event=%s)", task_id, event.type.value)
                return
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        except asyncio.CancelledError:
            logger.info("SSE stream cancelled for task %s", task_id)
            return
        except Exception:
            logger.exception("SSE stream error for task %s", task_id)
            return
