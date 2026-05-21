/**
 * AG-UI event type definitions for TypeScript.
 *
 * These mirror the backend Pydantic models in backend/v2/agui/events.py.
 */

// ============================================================================
// Event Type Enum
// ============================================================================

export enum AguiEventType {
  RUN_STARTED = 'RUN_STARTED',
  RUN_FINISHED = 'RUN_FINISHED',
  RUN_ERROR = 'RUN_ERROR',
  TEXT_MESSAGE_START = 'TEXT_MESSAGE_START',
  TEXT_MESSAGE_CONTENT = 'TEXT_MESSAGE_CONTENT',
  TEXT_MESSAGE_END = 'TEXT_MESSAGE_END',
  TOOL_CALL_START = 'TOOL_CALL_START',
  TOOL_CALL_ARGS = 'TOOL_CALL_ARGS',
  TOOL_CALL_END = 'TOOL_CALL_END',
  STATE_UPDATE = 'STATE_UPDATE',
  STATE_SNAPSHOT = 'STATE_SNAPSHOT',
  CUSTOM = 'CUSTOM',
}

// ============================================================================
// Event Interfaces
// ============================================================================

export interface BaseAguiEvent {
  type: AguiEventType;
  timestamp: number;
  rawEvent?: Record<string, unknown>;
}

export interface RunStartedEvent extends BaseAguiEvent {
  type: AguiEventType.RUN_STARTED;
  runId: string;
  taskId: string;
  threadId?: string;
}

export interface RunFinishedEvent extends BaseAguiEvent {
  type: AguiEventType.RUN_FINISHED;
  runId: string;
  taskId: string;
}

export interface RunErrorEvent extends BaseAguiEvent {
  type: AguiEventType.RUN_ERROR;
  runId: string;
  taskId: string;
  message: string;
  code?: string;
}

export interface TextMessageStartEvent extends BaseAguiEvent {
  type: AguiEventType.TEXT_MESSAGE_START;
  messageId: string;
  role: 'assistant';
}

export interface TextMessageContentEvent extends BaseAguiEvent {
  type: AguiEventType.TEXT_MESSAGE_CONTENT;
  messageId: string;
  delta: string;
}

export interface TextMessageEndEvent extends BaseAguiEvent {
  type: AguiEventType.TEXT_MESSAGE_END;
  messageId: string;
}

export interface ToolCallStartEvent extends BaseAguiEvent {
  type: AguiEventType.TOOL_CALL_START;
  toolCallId: string;
  toolCallName: string;
  parentMessageId?: string;
}

export interface ToolCallArgsEvent extends BaseAguiEvent {
  type: AguiEventType.TOOL_CALL_ARGS;
  toolCallId: string;
  delta: string;
}

export interface ToolCallEndEvent extends BaseAguiEvent {
  type: AguiEventType.TOOL_CALL_END;
  toolCallId: string;
  result?: string;
}

export interface StateUpdateEvent extends BaseAguiEvent {
  type: AguiEventType.STATE_UPDATE;
  state: Record<string, unknown>;
}

export interface StateSnapshotEvent extends BaseAguiEvent {
  type: AguiEventType.STATE_SNAPSHOT;
  snapshot: Record<string, unknown>;
}

export interface CustomEvent extends BaseAguiEvent {
  type: AguiEventType.CUSTOM;
  name: string;
  value: Record<string, unknown>;
}

// ============================================================================
// Union Type
// ============================================================================

export type AguiEvent =
  | RunStartedEvent
  | RunFinishedEvent
  | RunErrorEvent
  | TextMessageStartEvent
  | TextMessageContentEvent
  | TextMessageEndEvent
  | ToolCallStartEvent
  | ToolCallArgsEvent
  | ToolCallEndEvent
  | StateUpdateEvent
  | StateSnapshotEvent
  | CustomEvent;

// ============================================================================
// Approval State (extracted from STATE_UPDATE events)
// ============================================================================

export interface ApprovalState {
  pending: boolean;
  callId: string;
  toolName: string;
  summary: string;
  approved?: boolean;
  options?: Array<{
    optionId: string;
    name: string;
    kind: string;
  }>;
  category?: string;
}
