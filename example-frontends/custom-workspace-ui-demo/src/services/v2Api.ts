/**
 * REST API client for AG-UI endpoints.
 */

const V2_BASE = 'http://localhost:8000/v2';

// ============================================================================
// Types
// ============================================================================

export interface CreateTaskOptions {
  title?: string;
  resumeSessionId?: string;
  mode?: string;
  model?: string;
  mcpServers?: Record<string, unknown>;
  agentCommand?: string[];
}

export interface CreateTaskResponse {
  taskId: string;
  agentSessionId: string;
  runUrl: string;
  eventsUrl: string;
  modes?: Array<{ id: string; name: string }>;
  models?: Array<{ id: string; name: string }>;
  currentModeId?: string;
}

export interface StartRunResponse {
  runId: string;
}

export interface ApprovalResponse {
  success: boolean;
  callId: string;
}

export interface TaskSummary {
  taskId: string;
  agentSessionId: string;
  cwd: string;
  title: string;
  createdAt: string;
  updatedAt: string;
  status: 'idle' | 'running' | 'error';
}

export interface TaskListResponse {
  tasks: TaskSummary[];
}

// ============================================================================
// Utility
// ============================================================================

async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  return response.json();
}

// ============================================================================
// Task CRUD
// ============================================================================

export async function createTask(
  cwd: string,
  options?: CreateTaskOptions,
): Promise<CreateTaskResponse> {
  return fetchJson(`${V2_BASE}/tasks`, {
    method: 'POST',
    body: JSON.stringify({ cwd, ...options }),
  });
}

export async function listTasks(): Promise<TaskListResponse> {
  return fetchJson(`${V2_BASE}/tasks`);
}

export async function listResumableTasks(): Promise<TaskListResponse> {
  return fetchJson(`${V2_BASE}/tasks/resumable`);
}

export async function deleteTask(taskId: string): Promise<{ success: boolean }> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}`, {
    method: 'DELETE',
  });
}

export async function updateTask(
  taskId: string,
  data: { title?: string },
): Promise<TaskSummary> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  });
}

// ============================================================================
// Run lifecycle
// ============================================================================

export interface MessageAttachmentPayload {
  name: string;
  type: 'image' | 'file';
  mimeType: string;
  data: string; // base64-encoded content
}

export async function startRun(
  taskId: string,
  messages: Array<{
    role: string;
    content: string;
    attachments?: MessageAttachmentPayload[];
  }>,
  config?: Record<string, unknown>,
): Promise<StartRunResponse> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}/run`, {
    method: 'POST',
    body: JSON.stringify({
      input: { messages },
      config: config ?? null,
    }),
  });
}

/**
 * Returns the SSE events URL for a given task and run.
 */
export function getEventsUrl(taskId: string, runId: string): string {
  return `${V2_BASE}/tasks/${taskId}/events?runId=${encodeURIComponent(runId)}`;
}

// ============================================================================
// Approval
// ============================================================================

export async function sendApproval(
  taskId: string,
  callId: string,
  approved: boolean,
  optionId?: string,
): Promise<ApprovalResponse> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}/approval`, {
    method: 'POST',
    body: JSON.stringify({ callId, approved, optionId: optionId ?? null }),
  });
}

// ============================================================================
// Message history
// ============================================================================

export interface HistoryMessage {
  role: 'user' | 'agent' | 'tool';
  content: string;
  toolName?: string;
  toolPurpose?: string;
  toolParameters?: Record<string, unknown>;
}

export async function getMessages(
  taskId: string,
): Promise<{ messages: HistoryMessage[] }> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}/messages`);
}

// ============================================================================
// Mode / Model / Command
// ============================================================================

export async function setMode(
  taskId: string,
  modeId: string,
): Promise<{ success: boolean; modeId: string }> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}/mode`, {
    method: 'POST',
    body: JSON.stringify({ modeId }),
  });
}

export async function setModel(
  taskId: string,
  modelId: string,
): Promise<{ success: boolean; modelId: string }> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}/model`, {
    method: 'POST',
    body: JSON.stringify({ modelId }),
  });
}

// ============================================================================
// Command execution
// ============================================================================

export async function executeCommand(
  taskId: string,
  command: string,
  args?: Record<string, unknown>,
): Promise<{ success: boolean; command: string }> {
  return fetchJson(`${V2_BASE}/tasks/${taskId}/command`, {
    method: 'POST',
    body: JSON.stringify({ command, args: args ?? null }),
  });
}
