/**
 * useAgUiStream — React hook that manages AG-UI SSE stream lifecycle.
 *
 * Connects to the SSE endpoint, parses AG-UI events, and dispatches
 * them to the Zustand store.
 */

import { useCallback, useRef, useState } from 'react';
import { connectAguiStream, disconnectAguiStream } from '../services/aguiClient';
import { AguiEventType, type AguiEvent, type ApprovalState } from '../services/aguiTypes';
import * as v2Api from '../services/v2Api';
import { useSessionStore, type MessageAttachment } from '../stores/sessionStore';

export interface UseAgUiStreamReturn {
  /** Start a new run: POST /v2/tasks/{taskId}/run then connect SSE. */
  startRun: (taskId: string, text: string, attachments?: MessageAttachment[]) => Promise<void>;
  /** Cancel the active SSE stream. */
  cancelRun: () => void;
  /** Send an approval decision via REST. */
  sendApproval: (taskId: string, callId: string, approved: boolean, optionId?: string) => Promise<void>;
  /** Whether an SSE stream is currently active. */
  isStreaming: boolean;
}

/**
 * Generate a short title from the user's message text.
 * Truncates to ~50 chars at a word boundary.
 */
function generateTitleFromMessage(text: string): string {
  const cleaned = text.replace(/\s+/g, ' ').trim();
  if (cleaned.length <= 50) return cleaned;
  const truncated = cleaned.substring(0, 50);
  const lastSpace = truncated.lastIndexOf(' ');
  return (lastSpace > 20 ? truncated.substring(0, lastSpace) : truncated) + '…';
}

export function useAgUiStream(): UseAgUiStreamReturn {
  const [isStreaming, setIsStreaming] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const store = useSessionStore;

  // ── Start run ───────────────────────────────────────────────────────────

  const startRun = useCallback(async (taskId: string, text: string, attachments?: MessageAttachment[]) => {
    // Cancel any existing stream
    if (controllerRef.current) {
      disconnectAguiStream(controllerRef.current);
      controllerRef.current = null;
    }

    // Add user message to store (with attachments for display)
    store.getState().appendMessage(taskId, {
      role: 'user',
      content: text,
      timestamp: Date.now(),
      attachments: attachments?.map((a) => ({
        id: a.id,
        name: a.name,
        type: a.type,
        mimeType: a.mimeType,
        size: a.size,
        previewUrl: a.previewUrl,
      })),
    });

    // Auto-generate title from first user message if still default
    const session = store.getState().sessions[taskId];
    if (session && session.title === 'New Task') {
      const newTitle = generateTitleFromMessage(text);
      store.getState().updateSession(taskId, { title: newTitle });
      // Persist to backend (fire-and-forget)
      v2Api.updateTask(taskId, { title: newTitle }).catch((err) =>
        console.warn('[useAgUiStream] Failed to persist title:', err),
      );
    }

    // Build attachment payloads (base64-encoded)
    const attachmentPayloads: v2Api.MessageAttachmentPayload[] = (attachments ?? [])
      .filter((a) => a.base64)
      .map((a) => ({
        name: a.name,
        type: a.type,
        mimeType: a.mimeType,
        data: a.base64!,
      }));

    // Start the run via REST
    const { runId } = await v2Api.startRun(taskId, [
      {
        role: 'user',
        content: text,
        ...(attachmentPayloads.length > 0 ? { attachments: attachmentPayloads } : {}),
      },
    ]);

    // Update session with run ID
    store.getState().updateSession(taskId, { currentRunId: runId });

    // Connect to SSE
    const eventsUrl = v2Api.getEventsUrl(taskId, runId);
    setIsStreaming(true);

    const controller = connectAguiStream(eventsUrl, {
      onEvent: (event) => handleEvent(taskId, event),
      onError: (err) => {
        console.error('[useAgUiStream] SSE error:', err);
        store.getState().appendMessage(taskId, {
          role: 'agent',
          content: `Error: ${err.message}`,
          timestamp: Date.now(),
          isError: true,
        });
        store.getState().setSessionStatus(taskId, 'idle');
        setIsStreaming(false);
      },
      onClose: () => {
        setIsStreaming(false);
        controllerRef.current = null;
      },
    });

    controllerRef.current = controller;
  }, []);

  // ── Cancel run ──────────────────────────────────────────────────────────

  const cancelRun = useCallback(() => {
    if (controllerRef.current) {
      disconnectAguiStream(controllerRef.current);
      controllerRef.current = null;
      setIsStreaming(false);
    }
  }, []);

  // ── Send approval ─────────────────────────────────────────────────────

  const sendApproval = useCallback(
    async (taskId: string, callId: string, approved: boolean, optionId?: string) => {
      await v2Api.sendApproval(taskId, callId, approved, optionId);
      store.getState().removePendingApproval(taskId, callId);
    },
    [],
  );

  // ── Event handler ─────────────────────────────────────────────────────

  const handleEvent = useCallback((taskId: string, event: AguiEvent) => {
    const s = store.getState();

    switch (event.type) {
      case AguiEventType.RUN_STARTED:
        s.setSessionStatus(taskId, 'working');
        // Reset turn phase tracking for thinking detection
        s.updateSession(taskId, {
          _turnHasToolCall: false,
          _thinkingStartedAt: null,
          _candidateThinkingMsgIdx: null,
        });
        break;

      case AguiEventType.TEXT_MESSAGE_START: {
        const session = s.getSession(taskId);
        // Always create as a normal message (NOT thinking).
        // If this is the first text message in the turn (no tool calls yet),
        // record its index as a candidate — it will be retroactively marked
        // as thinking only if TOOL_CALL_START arrives later.
        s.appendMessage(taskId, {
          role: 'agent',
          content: '',
          timestamp: Date.now(),
        });
        if (session && !session._turnHasToolCall && session._candidateThinkingMsgIdx === null) {
          const updatedSession = s.getSession(taskId);
          const candidateIdx = updatedSession ? updatedSession.messages.length - 1 : null;
          s.updateSession(taskId, {
            _candidateThinkingMsgIdx: candidateIdx,
            _thinkingStartedAt: Date.now(),
          });
        }
        break;
      }

      case AguiEventType.TEXT_MESSAGE_CONTENT:
        s.appendToLastMessage(taskId, event.delta);
        break;

      case AguiEventType.TEXT_MESSAGE_END:
        // Nothing to do here — thinking is determined retroactively on TOOL_CALL_START
        break;

      case AguiEventType.TOOL_CALL_START: {
        // Retroactively mark the candidate message as "thinking" now that we know
        // a tool call follows the initial text
        const sess = s.getSession(taskId);
        if (sess && sess._candidateThinkingMsgIdx !== null) {
          const messages = [...sess.messages];
          const candidateMsg = messages[sess._candidateThinkingMsgIdx];
          if (candidateMsg?.role === 'agent') {
            const durationMs = sess._thinkingStartedAt ? Date.now() - sess._thinkingStartedAt : undefined;
            messages[sess._candidateThinkingMsgIdx] = {
              ...candidateMsg,
              isThinking: true,
              thinkingDurationMs: durationMs,
            };
            s.setMessages(taskId, messages);
          }
          s.updateSession(taskId, {
            _turnHasToolCall: true,
            _candidateThinkingMsgIdx: null,
            _thinkingStartedAt: null,
          });
        } else {
          s.updateSession(taskId, { _turnHasToolCall: true });
        }
        s.addToolCall(taskId, {
          toolCallId: event.toolCallId,
          toolName: event.toolCallName,
          parameters: {},
          status: 'running',
        });
        break;
      }

      case AguiEventType.TOOL_CALL_ARGS:
        s.appendToolArgs(taskId, event.toolCallId, event.delta);
        break;

      case AguiEventType.TOOL_CALL_END:
        s.updateToolCall(taskId, event.toolCallId, {
          status: 'completed',
          result: event.result,
        });
        break;

      case AguiEventType.STATE_UPDATE: {
        const approval = event.state.approval as ApprovalState | undefined;
        if (approval) {
          if (approval.pending) {
            s.addPendingApproval(taskId, {
              toolCallId: approval.callId,
              toolName: approval.toolName,
              parameters: {},
              status: 'pending',
              requiresApproval: true,
              purpose: approval.summary,
              permissionOptions: approval.options?.map((o) => ({
                id: o.optionId,
                label: o.name,
                kind: o.kind as 'allow_once' | 'allow_always' | 'reject_once' | 'reject_always',
              })),
            });
          } else {
            s.removePendingApproval(taskId, approval.callId);
          }
        }
        break;
      }

      case AguiEventType.RUN_FINISHED:
        s.finalizeTurn(taskId);
        break;

      case AguiEventType.RUN_ERROR:
        s.appendMessage(taskId, {
          role: 'agent',
          content: `Error: ${event.message}`,
          timestamp: Date.now(),
          isError: true,
        });
        s.setSessionStatus(taskId, 'idle');
        break;

      case AguiEventType.CUSTOM:
        _handleCustomEvent(taskId, event.name, event.value, s);
        break;

      default:
        console.debug('[useAgUiStream] Unhandled event type:', event.type);
    }
  }, []);

  return { startRun, cancelRun, sendApproval, isStreaming };
}

// ── Custom event handlers ─────────────────────────────────────────────────

function _handleCustomEvent(
  taskId: string,
  name: string,
  value: Record<string, unknown>,
  s: ReturnType<typeof useSessionStore.getState>,
) {
  switch (name) {
    case 'agent:mcp_initialized':
      s.addMcpServer(taskId, (value.serverName as string) || (value.name as string) || 'unknown');
      break;

    case 'agent:commands_available': {
      const commands = (value.commands || value.options || []) as Array<{
        name?: string;
        command?: string;
        description?: string;
      }>;
      s.setAvailableCommands(
        taskId,
        commands.map((c) => ({
          name: c.name || c.command || '',
          description: c.description,
        })),
      );
      break;
    }

    case 'agent:mcp_oauth': {
      const url = value.url as string;
      if (url) {
        window.open(url, '_blank');
      }
      break;
    }

    case 'agent:mode_update':
      s.updateSession(taskId, { currentModeId: (value.modeId as string) || 'default' });
      break;

    case 'agent:metadata': {
      // Context usage percentage and other metadata
      const contextUsage = (value.contextUsagePercent ?? value.usage) as number | undefined;
      s.setMetadata(taskId, {
        contextUsagePercent: contextUsage,
        ...value,
      });
      break;
    }

    case 'agent:compaction': {
      const status = (value.status as string) || 'completed';
      s.appendMessage(taskId, {
        role: 'agent',
        content: `Context compacted (${status}). Conversation history has been summarized to free up context space.`,
        timestamp: Date.now(),
      });
      console.debug(`[useAgUiStream] Context compaction: ${status}`, value);
      break;
    }

    case 'agent:clear': {
      // Clear conversation history
      s.setMessages(taskId, []);
      console.debug('[useAgUiStream] Conversation cleared', value);
      break;
    }

    case 'agent:subagent_terminated': {
      console.debug('[useAgUiStream] Sub-agent terminated:', value);
      break;
    }

    default:
      console.debug(`[useAgUiStream] Unknown CUSTOM event: ${name}`, value);
  }
}
