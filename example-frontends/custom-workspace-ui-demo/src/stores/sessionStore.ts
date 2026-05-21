/**
 * Session Store — Zustand store for AG-UI-based task management.
 *
 * Keeps the same component-facing API (addSession, appendMessage, etc.)
 * but internally keyed by taskId.
 */

import { create } from 'zustand';

// ============================================================================
// Types (same API surface as v1 sessionStore)
// ============================================================================

export type SessionStatus = 'idle' | 'working' | 'awaiting_approval';

export interface MessageAttachment {
  id: string;
  name: string;
  type: 'image' | 'file';
  mimeType: string;
  size: number;
  previewUrl?: string;   // object URL or data URL for image thumbnails
  base64?: string;       // base64-encoded file content
}

export interface ChatMessage {
  role: 'user' | 'agent' | 'tool';
  content: string;
  timestamp: number;
  toolCall?: ToolCall;
  isError?: boolean;
  isThinking?: boolean;         // marks this message as "thinking" content
  thinkingDurationMs?: number;  // elapsed time for thinking phase in ms
  attachments?: MessageAttachment[];  // file/image attachments
}

export interface ToolCall {
  toolCallId: string;
  toolName: string;
  parameters: Record<string, unknown>;
  status: 'pending' | 'running' | 'completed' | 'failed';
  result?: string;
  requiresApproval?: boolean;
  permissionOptions?: PermissionOption[];
  purpose?: string;
}

export interface PermissionOption {
  id: string;
  label: string;
  description?: string;
  kind?: 'allow_once' | 'allow_always' | 'reject_once' | 'reject_always';
}

export interface Session {
  id: string; // taskId
  title: string;
  cwd: string;
  status: SessionStatus;
  model: string;
  messages: ChatMessage[];
  createdAt: string;
  updatedAt: string;
  // Session-specific
  agentSessionId: string;
  currentRunId?: string;
  // Metadata
  modes: Array<{ id: string; name: string; description?: string }>;
  models: Array<{ id: string; name: string; description?: string }>;
  currentModeId: string;
  mcpServers: string[];
  availableCommands: Array<{ name: string; description?: string }>;
  // Approval state
  pendingApprovals: ToolCall[];
  // Metadata (context usage, etc.)
  metadata: {
    contextUsagePercent?: number;
    [key: string]: unknown;
  };
  // Accumulated tool args (for streaming TOOL_CALL_ARGS deltas)
  _toolArgsBuffer: Record<string, string>;
  // Turn phase tracking for thinking detection
  _turnHasToolCall: boolean;
  _thinkingStartedAt: number | null;
  _candidateThinkingMsgIdx: number | null; // index of first text msg in turn (may become thinking retroactively)
}

// ============================================================================
// Store State & Actions
// ============================================================================

interface SessionState {
  sessions: Record<string, Session>;
  activeSessionId: string | null;

  // Session management
  addSession: (session: Session) => void;
  removeSession: (id: string) => void;
  setActiveSession: (id: string | null) => void;
  updateSession: (id: string, updates: Partial<Session>) => void;

  // Status
  setSessionStatus: (id: string, status: SessionStatus) => void;

  // Messages
  appendMessage: (id: string, message: ChatMessage) => void;
  appendToLastMessage: (id: string, chunk: string) => void;
  setMessages: (id: string, messages: ChatMessage[]) => void;

  // Tool calls
  addToolCall: (sessionId: string, toolCall: ToolCall) => void;
  updateToolCall: (sessionId: string, toolCallId: string, update: Partial<ToolCall>) => void;
  appendToolArgs: (sessionId: string, toolCallId: string, delta: string) => void;
  finalizeTurn: (sessionId: string) => void;

  // Approval flow
  addPendingApproval: (id: string, toolCall: ToolCall) => void;
  removePendingApproval: (id: string, toolCallId: string) => void;
  clearPendingApprovals: (id: string) => void;

  // MCP / Commands / Metadata
  addMcpServer: (id: string, serverName: string) => void;
  setAvailableCommands: (id: string, commands: Array<{ name: string; description?: string }>) => void;
  setMetadata: (id: string, data: Record<string, unknown>) => void;

  // Helpers
  getActiveSession: () => Session | undefined;
  getSession: (id: string) => Session | undefined;
}

// ============================================================================
// Implementation
// ============================================================================

export const useSessionStore = create<SessionState>((set, get) => ({
  sessions: {},
  activeSessionId: null,

  // Session management
  addSession: (session) =>
    set((state) => ({
      sessions: { ...state.sessions, [session.id]: session },
    })),

  removeSession: (id) =>
    set((state) => {
      const { [id]: _, ...rest } = state.sessions;
      return {
        sessions: rest,
        activeSessionId: state.activeSessionId === id ? null : state.activeSessionId,
      };
    }),

  setActiveSession: (id) => set({ activeSessionId: id }),

  updateSession: (id, updates) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: { ...state.sessions, [id]: { ...session, ...updates } },
      };
    }),

  // Status
  setSessionStatus: (id, status) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: { ...state.sessions, [id]: { ...session, status } },
      };
    }),

  // Messages
  appendMessage: (id, message) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            messages: [...session.messages, message],
            updatedAt: new Date().toISOString(),
          },
        },
      };
    }),

  appendToLastMessage: (id, chunk) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session || session.messages.length === 0) return state;
      const messages = [...session.messages];
      const last = messages[messages.length - 1];
      if (last.role === 'agent') {
        messages[messages.length - 1] = { ...last, content: last.content + chunk };
        return {
          sessions: { ...state.sessions, [id]: { ...session, messages } },
        };
      }
      return state;
    }),

  setMessages: (id, messages) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: { ...state.sessions, [id]: { ...session, messages } },
      };
    }),

  // Tool calls
  addToolCall: (sessionId, toolCall) =>
    set((state) => {
      const session = state.sessions[sessionId];
      if (!session) return state;
      const message: ChatMessage = {
        role: 'tool',
        content: toolCall.purpose || `Tool: ${toolCall.toolName}`,
        timestamp: Date.now(),
        toolCall,
      };
      let pendingApprovals = session.pendingApprovals;
      if (toolCall.requiresApproval && !pendingApprovals.some((p) => p.toolCallId === toolCall.toolCallId)) {
        pendingApprovals = [...pendingApprovals, toolCall];
      }
      return {
        sessions: {
          ...state.sessions,
          [sessionId]: {
            ...session,
            messages: [...session.messages, message],
            pendingApprovals,
            status: pendingApprovals.length > 0 ? 'awaiting_approval' : session.status,
          },
        },
      };
    }),

  updateToolCall: (sessionId, toolCallId, update) =>
    set((state) => {
      const session = state.sessions[sessionId];
      if (!session) return state;
      const messages = session.messages.map((msg) => {
        if (msg.role === 'tool' && msg.toolCall?.toolCallId === toolCallId) {
          return { ...msg, toolCall: { ...msg.toolCall, ...update } };
        }
        return msg;
      });
      let pendingApprovals = session.pendingApprovals;
      if (update.status === 'completed' || update.status === 'failed') {
        pendingApprovals = pendingApprovals.filter((p) => p.toolCallId !== toolCallId);
      }
      return {
        sessions: {
          ...state.sessions,
          [sessionId]: {
            ...session,
            messages,
            pendingApprovals,
            status: pendingApprovals.length > 0 ? 'awaiting_approval' : session.status === 'awaiting_approval' ? 'working' : session.status,
          },
        },
      };
    }),

  appendToolArgs: (sessionId, toolCallId, delta) =>
    set((state) => {
      const session = state.sessions[sessionId];
      if (!session) return state;
      const buf = { ...session._toolArgsBuffer };
      buf[toolCallId] = (buf[toolCallId] || '') + delta;
      // Also update the tool call parameters in messages
      let params: Record<string, unknown> = {};
      try {
        params = JSON.parse(buf[toolCallId]);
      } catch {
        // Not complete JSON yet — that's fine
      }
      const messages = session.messages.map((msg) => {
        if (msg.role === 'tool' && msg.toolCall?.toolCallId === toolCallId) {
          return { ...msg, toolCall: { ...msg.toolCall, parameters: params } };
        }
        return msg;
      });
      return {
        sessions: {
          ...state.sessions,
          [sessionId]: { ...session, messages, _toolArgsBuffer: buf },
        },
      };
    }),

  finalizeTurn: (sessionId) =>
    set((state) => {
      const session = state.sessions[sessionId];
      if (!session) return state;
      return {
        sessions: {
          ...state.sessions,
          [sessionId]: {
            ...session,
            status: 'idle',
            pendingApprovals: [],
            _toolArgsBuffer: {},
          },
        },
      };
    }),

  // Approval flow
  addPendingApproval: (id, toolCall) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      if (session.pendingApprovals.some((p) => p.toolCallId === toolCall.toolCallId)) return state;
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            pendingApprovals: [...session.pendingApprovals, toolCall],
            status: 'awaiting_approval',
          },
        },
      };
    }),

  removePendingApproval: (id, toolCallId) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      const pendingApprovals = session.pendingApprovals.filter((p) => p.toolCallId !== toolCallId);
      return {
        sessions: {
          ...state.sessions,
          [id]: {
            ...session,
            pendingApprovals,
            status: pendingApprovals.length > 0 ? 'awaiting_approval' : 'working',
          },
        },
      };
    }),

  clearPendingApprovals: (id) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: {
          ...state.sessions,
          [id]: { ...session, pendingApprovals: [], status: 'idle' },
        },
      };
    }),

  // MCP / Commands
  addMcpServer: (id, serverName) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session || session.mcpServers.includes(serverName)) return state;
      return {
        sessions: {
          ...state.sessions,
          [id]: { ...session, mcpServers: [...session.mcpServers, serverName] },
        },
      };
    }),

  setAvailableCommands: (id, commands) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: {
          ...state.sessions,
          [id]: { ...session, availableCommands: commands },
        },
      };
    }),

  setMetadata: (id, data) =>
    set((state) => {
      const session = state.sessions[id];
      if (!session) return state;
      return {
        sessions: {
          ...state.sessions,
          [id]: { ...session, metadata: { ...session.metadata, ...data } },
        },
      };
    }),

  // Helpers
  getActiveSession: () => {
    const state = get();
    if (!state.activeSessionId) return undefined;
    return state.sessions[state.activeSessionId];
  },

  getSession: (id) => get().sessions[id],
}));

// ============================================================================
// Selectors
// ============================================================================

export const selectActiveSession = (state: SessionState) => {
  if (!state.activeSessionId) return undefined;
  return state.sessions[state.activeSessionId];
};

export const selectSessionsList = (state: SessionState) =>
  Object.values(state.sessions);

/**
 * Create a fresh Session object for a newly created task.
 */
export function createSessionFromTask(
  taskId: string,
  agentSessionId: string,
  cwd: string,
  opts?: {
    title?: string;
    modes?: Array<{ id: string; name: string }>;
    models?: Array<{ id: string; name: string }>;
    currentModeId?: string;
  },
): Session {
  return {
    id: taskId,
    title: opts?.title || 'New Task',
    cwd,
    status: 'idle',
    model: 'auto',
    messages: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
    agentSessionId,
    modes: opts?.modes?.map((m) => ({ ...m })) ?? [],
    models: opts?.models?.map((m) => ({ ...m })) ?? [],
    currentModeId: opts?.currentModeId ?? 'default',
    mcpServers: [],
    availableCommands: [],
    pendingApprovals: [],
    metadata: {},
    _toolArgsBuffer: {},
    _turnHasToolCall: false,
    _thinkingStartedAt: null,
    _candidateThinkingMsgIdx: null,
  };
}
