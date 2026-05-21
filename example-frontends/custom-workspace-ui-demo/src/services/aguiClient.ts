/**
 * AG-UI SSE stream client.
 *
 * Connects to the /v2/tasks/{taskId}/events SSE endpoint and parses
 * AG-UI events, dispatching them to a callback.
 *
 * Uses fetch + ReadableStream (not EventSource) because EventSource
 * only supports GET and we need custom headers in the future.
 */

import { type AguiEvent, AguiEventType } from './aguiTypes';

export interface AguiStreamOptions {
  /** Called for each parsed AG-UI event. */
  onEvent: (event: AguiEvent) => void;
  /** Called when the stream encounters an error. */
  onError: (error: Error) => void;
  /** Called when the stream closes (after RUN_FINISHED/RUN_ERROR or abort). */
  onClose?: () => void;
}

/**
 * Connect to an AG-UI SSE stream.
 *
 * @param eventsUrl  Full URL to the SSE endpoint.
 * @param options    Callbacks for events, errors, and close.
 * @returns An AbortController — call `.abort()` to disconnect.
 */
export function connectAguiStream(
  eventsUrl: string,
  options: AguiStreamOptions,
): AbortController {
  const controller = new AbortController();

  // Start reading in the background
  _readStream(eventsUrl, controller.signal, options).catch((err) => {
    if (err.name !== 'AbortError') {
      options.onError(err instanceof Error ? err : new Error(String(err)));
    }
  });

  return controller;
}

/**
 * Disconnect an active stream.
 */
export function disconnectAguiStream(controller: AbortController): void {
  controller.abort();
}

// ── Internal ──────────────────────────────────────────────────────────────

async function _readStream(
  url: string,
  signal: AbortSignal,
  { onEvent, onError, onClose }: AguiStreamOptions,
): Promise<void> {
  const response = await fetch(url, { signal });

  if (!response.ok) {
    throw new Error(`SSE connection failed: HTTP ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('Response body is not readable');
  }

  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // Process complete SSE blocks (separated by double newline)
      const blocks = buffer.split('\n\n');
      // Keep the last incomplete block in the buffer
      buffer = blocks.pop() ?? '';

      for (const block of blocks) {
        if (!block.trim()) continue;

        // Skip SSE comments (keepalives)
        if (block.trim().startsWith(':')) continue;

        // Parse SSE fields
        let dataLine = '';
        for (const line of block.split('\n')) {
          if (line.startsWith('data: ')) {
            dataLine += line.slice(6);
          } else if (line.startsWith('data:')) {
            dataLine += line.slice(5);
          }
        }

        if (!dataLine) continue;

        try {
          const event = JSON.parse(dataLine) as AguiEvent;
          onEvent(event);

          // Terminal events — stream will close after this
          if (
            event.type === AguiEventType.RUN_FINISHED ||
            event.type === AguiEventType.RUN_ERROR
          ) {
            onClose?.();
            return;
          }
        } catch (parseErr) {
          console.warn('[aguiClient] Failed to parse SSE data:', dataLine, parseErr);
        }
      }
    }
  } finally {
    reader.releaseLock();
    onClose?.();
  }
}
