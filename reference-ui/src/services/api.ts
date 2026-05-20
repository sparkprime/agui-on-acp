/**
 * REST API client for the ACP bridge backend.
 *
 * Provides access to side-channel APIs: files, git.
 * Task/session management is handled by v2Api.ts via AG-UI endpoints.
 */

const API_BASE = 'http://localhost:8000/api';

// ============================================================================
// Types
// ============================================================================

export interface FileItem {
  name: string;
  path: string;
  isDirectory: boolean;
  size?: number;
  modifiedTime?: number;
}

export interface GitStatus {
  branch: string;
  ahead: number;
  behind: number;
  files: GitStatusFile[];
  isRepo: boolean;
}

export interface GitStatusFile {
  status: string;
  path: string;
  staged: boolean;
}

export interface GitLogEntry {
  hash: string;
  shortHash: string;
  message: string;
  author: string;
  date: string;
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
// Health
// ============================================================================

export async function healthCheck(): Promise<{ status: string; version: string }> {
  return fetchJson('http://localhost:8000/health');
}

// ============================================================================
// Files
// ============================================================================

export async function listFiles(
  path: string = '.',
  base: string = '.'
): Promise<{ items: FileItem[]; path: string }> {
  const params = new URLSearchParams({ path, base });
  return fetchJson(`${API_BASE}/files?${params}`);
}

export async function readFile(
  path: string,
  base: string = '.'
): Promise<{ content: string; path: string }> {
  const params = new URLSearchParams({ path, base });
  return fetchJson(`${API_BASE}/files/content?${params}`);
}

export async function writeFile(
  path: string,
  content: string,
  base: string = '.'
): Promise<{ success: boolean; path: string }> {
  const params = new URLSearchParams({ base });
  return fetchJson(`${API_BASE}/files?${params}`, {
    method: 'POST',
    body: JSON.stringify({ path, content }),
  });
}

export async function updateFile(
  path: string,
  content: string,
  base: string = '.'
): Promise<{ success: boolean; path: string }> {
  const params = new URLSearchParams({ base });
  return fetchJson(`${API_BASE}/files?${params}`, {
    method: 'PUT',
    body: JSON.stringify({ path, content }),
  });
}

export async function deleteFile(
  path: string,
  base: string = '.'
): Promise<{ success: boolean; path: string }> {
  const params = new URLSearchParams({ path, base });
  return fetchJson(`${API_BASE}/files?${params}`, {
    method: 'DELETE',
  });
}

export async function createDirectory(
  path: string,
  base: string = '.'
): Promise<{ success: boolean; path: string }> {
  const params = new URLSearchParams({ path, base });
  return fetchJson(`${API_BASE}/files/mkdir?${params}`, {
    method: 'POST',
  });
}

export async function renamePath(
  oldPath: string,
  newPath: string,
  base: string = '.'
): Promise<{ success: boolean }> {
  const params = new URLSearchParams({ base });
  return fetchJson(`${API_BASE}/files/rename?${params}`, {
    method: 'POST',
    body: JSON.stringify({ old_path: oldPath, new_path: newPath }),
  });
}

export async function readFileAsBlob(
  path: string,
  base: string = '.'
): Promise<Blob> {
  const params = new URLSearchParams({ path, base });
  const response = await fetch(`${API_BASE}/files/blob?${params}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch file: ${response.statusText}`);
  }
  return response.blob();
}

// ============================================================================
// Git
// ============================================================================

export async function getGitStatus(dir: string = '.'): Promise<GitStatus> {
  const params = new URLSearchParams({ dir });
  return fetchJson(`${API_BASE}/git/status?${params}`);
}

export async function getGitLog(
  dir: string = '.',
  limit: number = 50
): Promise<{ entries: GitLogEntry[] }> {
  const params = new URLSearchParams({ dir, limit: String(limit) });
  return fetchJson(`${API_BASE}/git/log?${params}`);
}

export async function getGitDiff(
  dir: string = '.',
  staged: boolean = false,
  file?: string
): Promise<{ diff: string }> {
  const params = new URLSearchParams({ dir, staged: String(staged) });
  if (file) params.set('file', file);
  return fetchJson(`${API_BASE}/git/diff?${params}`);
}

export async function gitCommit(
  message: string,
  dir: string = '.',
  files?: string[]
): Promise<{ success: boolean; hash?: string; message?: string }> {
  return fetchJson(`${API_BASE}/git/commit`, {
    method: 'POST',
    body: JSON.stringify({ dir, message, files }),
  });
}

export async function gitStage(
  file: string,
  dir: string = '.'
): Promise<{ success: boolean; file: string }> {
  const params = new URLSearchParams({ dir, file });
  return fetchJson(`${API_BASE}/git/stage?${params}`, {
    method: 'POST',
  });
}

export async function gitUnstage(
  file: string,
  dir: string = '.'
): Promise<{ success: boolean; file: string }> {
  const params = new URLSearchParams({ dir, file });
  return fetchJson(`${API_BASE}/git/unstage?${params}`, {
    method: 'POST',
  });
}

export async function gitDiscard(
  file: string,
  dir: string = '.'
): Promise<{ success: boolean; file: string }> {
  const params = new URLSearchParams({ dir, file });
  return fetchJson(`${API_BASE}/git/discard?${params}`, {
    method: 'POST',
  });
}

export async function getGitBranches(
  dir: string = '.'
): Promise<{ branches: Array<{ name: string; isRemote: boolean; isCurrent: boolean }>; current: string }> {
  const params = new URLSearchParams({ dir });
  return fetchJson(`${API_BASE}/git/branches?${params}`);
}

// ============================================================================
// Path Suggestions (for @path autocomplete)
// ============================================================================

export interface PathSuggestion {
  name: string;
  path: string;
  isDirectory: boolean;
}

export async function fetchPathSuggestions(
  path: string,
  cwd: string
): Promise<PathSuggestion[]> {
  const lastSlash = path.lastIndexOf('/');
  const parentPath = lastSlash >= 0 ? path.slice(0, lastSlash) || '.' : '.';
  const partial = lastSlash >= 0 ? path.slice(lastSlash + 1) : path;

  try {
    const { items } = await listFiles(parentPath, cwd);

    const filtered = items.filter((item) =>
      item.name.toLowerCase().startsWith(partial.toLowerCase())
    );

    filtered.sort((a, b) => {
      if (a.isDirectory !== b.isDirectory) {
        return a.isDirectory ? -1 : 1;
      }
      return a.name.localeCompare(b.name);
    });

    return filtered.slice(0, 10).map((item) => ({
      name: item.name,
      path: lastSlash >= 0 ? `${parentPath}/${item.name}` : item.name,
      isDirectory: item.isDirectory,
    }));
  } catch (error) {
    console.error('[api] Failed to fetch path suggestions:', error);
    return [];
  }
}
