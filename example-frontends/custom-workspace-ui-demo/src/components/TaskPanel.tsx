/**
 * TaskPanel Component - Kanban-style Task Board
 *
 * Displays all tasks (sessions) in three columns:
 * - Active: Sessions with status 'working' or 'idle'
 * - Needs Approval: Sessions with status 'awaiting_approval'
 * - Inactive: Resumable sessions from disk (not yet loaded)
 *
 * Uses the Zustand session store (stores/sessionStore.ts) as the source of truth.
 */

import React, { useMemo, useState, useRef, useEffect } from 'react';
import {
  Play,
  Pause,
  Clock,
  AlertCircle,
  Archive,
  ListTodo,
  RefreshCw,
  ExternalLink,
  CheckCircle,
  X,
  ChevronDown,
  ChevronRight,
  Edit2,
  Check,
  Folder,
} from 'lucide-react';
import {
  useSessionStore,
  Session,
  SessionStatus,
} from '../stores/sessionStore';
import * as v2Api from '../services/v2Api';

// ============================================================================
// Types
// ============================================================================

interface TaskPanelProps {
  onSelectSession: (sessionId: string) => void;
  onResumeSession: (sessionId: string) => Promise<void>;
  onDeleteSession: (sessionId: string) => void;
  onRefreshResumable: () => Promise<void>;
  onApprove?: (sessionId: string, toolCallId: string, optionId: string) => void;
  onReject?: (sessionId: string, toolCallId: string) => void;
  // Layout mode: 'chat' = vertical collapsible, 'task' = horizontal grid
  layoutMode?: 'chat' | 'task';
}

// Default session title
const DEFAULT_SESSION_TITLE = 'agent';

type KanbanColumn = 'active' | 'needs_approval' | 'inactive';

// ============================================================================
// Helper Functions
// ============================================================================

function getColumnForSession(session: Session): KanbanColumn {
  if (session.status === 'awaiting_approval') return 'needs_approval';
  return 'active'; // 'working' or 'idle' are both active
}

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const now = new Date();
  const diff = now.getTime() - date.getTime();
  
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  const days = Math.floor(diff / 86400000);
  
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  if (hours < 24) return `${hours}h ago`;
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

function truncateText(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text;
  return text.substring(0, maxLength - 3) + '...';
}

// ============================================================================
// Column Header Component
// ============================================================================

interface ColumnHeaderProps {
  title: string;
  icon: React.ReactNode;
  count: number;
  colorClass: string;
  isCollapsible?: boolean;
  isExpanded?: boolean;
  onToggle?: () => void;
}

const ColumnHeader: React.FC<ColumnHeaderProps> = ({
  title,
  icon,
  count,
  colorClass,
  isCollapsible = false,
  isExpanded = true,
  onToggle,
}) => (
  <div
    className={`flex items-center gap-2 p-3 border-b ${colorClass} ${isCollapsible ? 'cursor-pointer hover:bg-white/5' : ''}`}
    onClick={isCollapsible ? onToggle : undefined}
  >
    {isCollapsible && (
      isExpanded ? <ChevronDown size={14} className="text-ide-text/50" /> : <ChevronRight size={14} className="text-ide-text/50" />
    )}
    {icon}
    <span className="font-semibold text-ide-textLight">{title}</span>
    <span className="ml-auto px-2 py-0.5 text-xs rounded-full bg-white/10 text-ide-text">
      {count}
    </span>
  </div>
);

// ============================================================================
// Session Card Component (Active & Needs Approval)
// ============================================================================

interface SessionCardProps {
  session: Session;
  isActive: boolean;
  onSelect: () => void;
  onDelete: () => void;
  onApprove?: () => void;
  onReject?: () => void;
}

const SessionCard: React.FC<SessionCardProps> = ({
  session,
  isActive,
  onSelect,
  onDelete,
  onApprove,
  onReject,
}) => {
  const [isEditing, setIsEditing] = useState(false);
  const [editTitle, setEditTitle] = useState(session.title);
  const inputRef = useRef<HTMLInputElement>(null);
  const updateSession = useSessionStore((s) => s.updateSession);
  
  // Get first user message as preview
  const firstUserMessage = session.messages.find((m) => m.role === 'user');
  const preview = firstUserMessage?.content || 'New session';
  
  // Extract directory name from cwd
  const dirName = session.cwd.split('/').pop() || session.cwd;
  
  // Display title: use session title, or default if it's 'New Chat'
  const displayTitle = session.title !== 'New Chat' ? session.title : DEFAULT_SESSION_TITLE;
  
  // Focus input when editing starts
  useEffect(() => {
    if (isEditing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [isEditing]);
  
  // Handle title save
  const handleSaveTitle = async () => {
    const newTitle = editTitle.trim() || DEFAULT_SESSION_TITLE;
    setIsEditing(false);
    
    if (newTitle !== session.title) {
      try {
        // Update in store immediately
        updateSession(session.id, { title: newTitle });
        // Persist to backend
        await v2Api.updateTask(session.id, { title: newTitle });
      } catch (error) {
        console.error('[TaskPanel] Failed to rename session:', error);
        // Revert on error
        updateSession(session.id, { title: session.title });
      }
    }
  };
  
  return (
    <div
      className={`
        group bg-ide-bg/50 rounded-sl-3 p-3 cursor-pointer
        hover:bg-ide-bg transition-colors border border-transparent
        ${isActive ? 'border-ide-accent/50 bg-ide-accent/5' : ''}
      `}
      onClick={onSelect}
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-2 mb-2">
        <div className="flex items-center gap-2 min-w-0 flex-1">
          <StatusIndicator status={session.status} />
          
          {isEditing ? (
            <input
              ref={inputRef}
              type="text"
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              onBlur={handleSaveTitle}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  handleSaveTitle();
                }
                if (e.key === 'Escape') {
                  setEditTitle(session.title);
                  setIsEditing(false);
                }
              }}
              onClick={(e) => e.stopPropagation()}
              className="flex-1 bg-ide-bg border border-ide-accent rounded px-1.5 py-0.5 text-sm text-ide-textLight focus:outline-none"
            />
          ) : (
            <span className="font-medium text-sm text-ide-textLight truncate">
              {displayTitle}
            </span>
          )}
        </div>
        
        {/* Actions - visible on hover */}
        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
          {isEditing ? (
            <button
              onClick={(e) => {
                e.stopPropagation();
                handleSaveTitle();
              }}
              className="p-1 hover:bg-green-500/20 rounded text-green-400"
              title="Save"
            >
              <Check size={14} />
            </button>
          ) : (
            <button
              onClick={(e) => {
                e.stopPropagation();
                setEditTitle(displayTitle);
                setIsEditing(true);
              }}
              className="p-1 hover:bg-white/10 rounded text-ide-text hover:text-ide-textLight"
              title="Edit title"
            >
              <Edit2 size={14} />
            </button>
          )}
          <button
            onClick={(e) => {
              e.stopPropagation();
              onDelete();
            }}
            className="p-1 hover:bg-red-500/20 rounded text-ide-text hover:text-red-400"
            title="Delete"
          >
            <X size={14} />
          </button>
        </div>
      </div>
      
      {/* Preview */}
      <p className="text-xs text-ide-text/70 mb-2 line-clamp-2">
        {truncateText(preview, 100)}
      </p>
      
      {/* Footer with full cwd path */}
      <div className="flex items-center gap-2 text-xs text-ide-text/50">
        <Folder size={12} className="flex-shrink-0" />
        <span className="truncate font-mono" title={session.cwd}>{session.cwd}</span>
        <span className="ml-auto flex-shrink-0">{formatRelativeTime(session.updatedAt)}</span>
      </div>
      
      {/* Approval Actions - show all pending approvals */}
      {session.status === 'awaiting_approval' && session.pendingApprovals?.length > 0 && (
        <div className="mt-3 pt-3 border-t border-ide-border/50 space-y-2">
          {session.pendingApprovals.map((pending) => (
            <div key={pending.toolCallId}>
              <div className="flex items-center gap-2 text-xs text-yellow-400 mb-1">
                <AlertCircle size={14} />
                <span className="font-medium">{pending.toolName}</span>
              </div>
              {pending.purpose && (
                <p className="text-xs text-ide-text/60 mb-2">
                  {truncateText(pending.purpose, 80)}
                </p>
              )}
              <div className="flex gap-2">
                {pending.permissionOptions?.slice(0, 2).map((opt) => (
                  <button
                    key={opt.id}
                    onClick={(e) => {
                      e.stopPropagation();
                      onApprove?.();
                    }}
                    className="flex-1 px-2 py-1 text-xs rounded bg-green-600/20 hover:bg-green-600/30 text-green-400"
                  >
                    {opt.label}
                  </button>
                ))}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    onReject?.();
                  }}
                  className="px-2 py-1 text-xs rounded bg-red-600/20 hover:bg-red-600/30 text-red-400"
                >
                  Reject
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
};

// ============================================================================
// Resumable Session Card Component (Inactive)
// ============================================================================


// ============================================================================
// Status Indicator
// ============================================================================

function StatusIndicator({ status }: { status: SessionStatus }) {
  const config: Record<SessionStatus, { color: string; icon: React.ReactNode }> = {
    idle: {
      color: 'bg-green-500',
      icon: <CheckCircle size={12} className="text-green-500" />,
    },
    working: {
      color: 'bg-yellow-500 animate-pulse',
      icon: <Play size={12} className="text-yellow-500" />,
    },
    awaiting_approval: {
      color: 'bg-red-500 animate-pulse',
      icon: <AlertCircle size={12} className="text-red-500" />,
    },
  };
  
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${config[status].color}`}
      title={status}
    />
  );
}

// ============================================================================
// Empty Column State
// ============================================================================

interface EmptyColumnProps {
  column: KanbanColumn;
}

const EmptyColumn: React.FC<EmptyColumnProps> = ({ column }) => {
  const messages: Record<KanbanColumn, { title: string; subtitle: string }> = {
    active: {
      title: 'No active tasks',
      subtitle: 'Start a new session to begin',
    },
    needs_approval: {
      title: 'No pending approvals',
      subtitle: 'All clear!',
    },
    inactive: {
      title: 'No recent sessions',
      subtitle: 'Previous sessions will appear here',
    },
  };
  
  return (
    <div className="flex flex-col items-center justify-center h-32 text-center">
      <p className="text-sm text-ide-text/50">{messages[column].title}</p>
      <p className="text-xs text-ide-text/30 mt-1">{messages[column].subtitle}</p>
    </div>
  );
};

// ============================================================================
// Main TaskPanel Component
// ============================================================================

export const TaskPanel: React.FC<TaskPanelProps> = ({
  onSelectSession,
  onResumeSession,
  onDeleteSession,
  onRefreshResumable,
  onApprove,
  onReject,
  layoutMode = 'task',
}) => {
  // Get data from Zustand store
  const sessionsMap = useSessionStore((s) => s.sessions);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  
  // Collapsible state for vertical layout (Active always expanded)
  const [needsApprovalExpanded, setNeedsApprovalExpanded] = useState(true);
  const [inactiveExpanded, setInactiveExpanded] = useState(false);
  
  // Derive sessions list
  const sessions = useMemo(() => Object.values(sessionsMap), [sessionsMap]);
  
  // Categorize sessions into columns
  const { activeTasks, needsApprovalTasks, inactiveTasks } = useMemo(() => {
    const active: Session[] = [];
    const needsApproval: Session[] = [];
    
    for (const session of sessions) {
      const column = getColumnForSession(session);
      if (column === 'needs_approval') {
        needsApproval.push(session);
      } else {
        active.push(session);
      }
    }
    
    // Sort by most recently updated
    active.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
    needsApproval.sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
    
    return {
      activeTasks: active,
      needsApprovalTasks: needsApproval,
      inactiveTasks: [] as Session[],
    };
  }, [sessions, sessionsMap]);
  
  // Statistics
  const stats = useMemo(() => ({
    total: sessions.length + inactiveTasks.length,
    active: activeTasks.length,
    needsApproval: needsApprovalTasks.length,
    inactive: inactiveTasks.length,
  }), [sessions, activeTasks, needsApprovalTasks, inactiveTasks]);
  
  return (
    <div className="h-full flex flex-col bg-ide-panel">
      {/* Header with Statistics */}
      <div className="p-4 border-b border-ide-border">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <ListTodo size={20} className="text-ide-accent" />
            <h2 className="text-lg font-semibold text-ide-textLight">Tasks</h2>
          </div>
          <div className="flex items-center gap-4 text-sm">
            <div className="flex items-center gap-1.5">
              <Play size={14} className="text-green-400" />
              <span className="font-medium text-ide-textLight">{stats.active}</span>
              <span className="text-ide-text">Active</span>
            </div>
            <div className="flex items-center gap-1.5">
              <AlertCircle size={14} className="text-yellow-400" />
              <span className="font-medium text-ide-textLight">{stats.needsApproval}</span>
              <span className="text-ide-text">Pending</span>
            </div>
            <div className="flex items-center gap-1.5">
              <Archive size={14} className="text-ide-text/50" />
              <span className="font-medium text-ide-textLight">{stats.inactive}</span>
              <span className="text-ide-text">Inactive</span>
            </div>
            <button
              onClick={onRefreshResumable}
              className="p-1.5 hover:bg-white/10 rounded text-ide-text hover:text-ide-textLight transition-colors"
              title="Refresh"
            >
              <RefreshCw size={14} />
            </button>
          </div>
        </div>
      </div>
      
      {/* Kanban Columns */}
      <div className="flex-1 overflow-auto p-4">
        {stats.total === 0 ? (
          <div className="h-full flex flex-col items-center justify-center text-center">
            <ListTodo size={48} className="text-ide-text/20 mb-4" />
            <h3 className="text-lg font-medium text-ide-textLight mb-2">No Tasks Yet</h3>
            <p className="text-sm text-ide-text max-w-md">
              Create a new session to start working on a task.
            </p>
          </div>
        ) : layoutMode === 'task' ? (
          /* Horizontal Grid Layout (Task Mode) */
          <div className="grid grid-cols-3 gap-4 min-h-[500px]">
            {/* Active Column */}
            <div className="flex flex-col rounded-sl-2 border border-green-500/20 bg-green-500/5">
              <ColumnHeader
                title="Active"
                icon={<Play size={16} className="text-green-400" />}
                count={activeTasks.length}
                colorClass="border-green-500/20"
              />
              <div className="flex-1 p-2 overflow-y-auto space-y-2">
                {activeTasks.length === 0 ? (
                  <EmptyColumn column="active" />
                ) : (
                  activeTasks.map((session) => (
                    <SessionCard
                      key={session.id}
                      session={session}
                      isActive={session.id === activeSessionId}
                      onSelect={() => onSelectSession(session.id)}
                      onDelete={() => onDeleteSession(session.id)}
                    />
                  ))
                )}
              </div>
            </div>
            
            {/* Needs Approval Column */}
            <div className="flex flex-col rounded-sl-2 border border-yellow-500/20 bg-yellow-500/5">
              <ColumnHeader
                title="Needs Approval"
                icon={<AlertCircle size={16} className="text-yellow-400" />}
                count={needsApprovalTasks.length}
                colorClass="border-yellow-500/20"
              />
              <div className="flex-1 p-2 overflow-y-auto space-y-2">
                {needsApprovalTasks.length === 0 ? (
                  <EmptyColumn column="needs_approval" />
                ) : (
                  needsApprovalTasks.map((session) => (
                    <SessionCard
                      key={session.id}
                      session={session}
                      isActive={session.id === activeSessionId}
                      onSelect={() => onSelectSession(session.id)}
                      onDelete={() => onDeleteSession(session.id)}
                      onApprove={() => {
                        if (session.pendingApprovals?.length && onApprove) {
                          for (const pending of session.pendingApprovals) {
                            const firstOption = pending.permissionOptions?.[0];
                            if (firstOption) {
                              onApprove(session.id, pending.toolCallId, firstOption.id);
                            }
                          }
                        }
                      }}
                      onReject={() => {
                        if (session.pendingApprovals?.length && onReject) {
                          for (const pending of session.pendingApprovals) {
                            onReject(session.id, pending.toolCallId);
                          }
                        }
                      }}
                    />
                  ))
                )}
              </div>
            </div>
            
            {/* Inactive Column */}
            <div className="flex flex-col rounded-sl-2 border border-ide-border bg-ide-bg/30">
              <ColumnHeader
                title="Inactive"
                icon={<Archive size={16} className="text-ide-text/50" />}
                count={inactiveTasks.length}
                colorClass="border-ide-border"
              />
              <div className="flex-1 p-2 overflow-y-auto space-y-2">
                {inactiveTasks.length === 0 ? (
                  <EmptyColumn column="inactive" />
                ) : (
                  inactiveTasks.slice(0, 20).map((session) => (
                    <SessionCard
                      key={session.id}
                      session={session}
                      isActive={false}
                      onSelect={() => onSelectSession(session.id)}
                      onDelete={() => onDeleteSession(session.id)}
                    />
                  ))
                )}
              </div>
            </div>
          </div>
        ) : (
          /* Vertical Collapsible Layout (Chat Mode) */
          <div className="flex flex-col gap-3">
            {/* Active Section - Always Expanded */}
            <div className="rounded-sl-2 border border-green-500/20 bg-green-500/5">
              <ColumnHeader
                title="Active"
                icon={<Play size={16} className="text-green-400" />}
                count={activeTasks.length}
                colorClass="border-green-500/20"
              />
              <div className="p-2 space-y-2">
                {activeTasks.length === 0 ? (
                  <div className="py-4 text-center text-sm text-ide-text/50">No active tasks</div>
                ) : (
                  activeTasks.map((session) => (
                    <SessionCard
                      key={session.id}
                      session={session}
                      isActive={session.id === activeSessionId}
                      onSelect={() => onSelectSession(session.id)}
                      onDelete={() => onDeleteSession(session.id)}
                    />
                  ))
                )}
              </div>
            </div>
            
            {/* Needs Approval Section - Collapsible */}
            <div className="rounded-sl-2 border border-yellow-500/20 bg-yellow-500/5">
              <ColumnHeader
                title="Needs Approval"
                icon={<AlertCircle size={16} className="text-yellow-400" />}
                count={needsApprovalTasks.length}
                colorClass="border-yellow-500/20"
                isCollapsible
                isExpanded={needsApprovalExpanded}
                onToggle={() => setNeedsApprovalExpanded(!needsApprovalExpanded)}
              />
              {needsApprovalExpanded && (
                <div className="p-2 space-y-2">
                  {needsApprovalTasks.length === 0 ? (
                    <div className="py-4 text-center text-sm text-ide-text/50">No pending approvals</div>
                  ) : (
                    needsApprovalTasks.map((session) => (
                      <SessionCard
                        key={session.id}
                        session={session}
                        isActive={session.id === activeSessionId}
                        onSelect={() => onSelectSession(session.id)}
                        onDelete={() => onDeleteSession(session.id)}
                        onApprove={() => {
                          if (session.pendingApprovals?.length && onApprove) {
                            for (const pending of session.pendingApprovals) {
                              const firstOption = pending.permissionOptions?.[0];
                              if (firstOption) {
                                onApprove(session.id, pending.toolCallId, firstOption.id);
                              }
                            }
                          }
                        }}
                        onReject={() => {
                          if (session.pendingApprovals?.length && onReject) {
                            for (const pending of session.pendingApprovals) {
                              onReject(session.id, pending.toolCallId);
                            }
                          }
                        }}
                      />
                    ))
                  )}
                </div>
              )}
            </div>
            
            {/* Inactive Section - Collapsible */}
            <div className="rounded-sl-2 border border-ide-border bg-ide-bg/30">
              <ColumnHeader
                title="Inactive"
                icon={<Archive size={16} className="text-ide-text/50" />}
                count={inactiveTasks.length}
                colorClass="border-ide-border"
                isCollapsible
                isExpanded={inactiveExpanded}
                onToggle={() => setInactiveExpanded(!inactiveExpanded)}
              />
              {inactiveExpanded && (
                <div className="p-2 space-y-2">
                  {inactiveTasks.length === 0 ? (
                    <div className="py-4 text-center text-sm text-ide-text/50">No recent sessions</div>
                  ) : (
                    inactiveTasks.slice(0, 10).map((session) => (
                      <SessionCard
                        key={session.id}
                        session={session}
                        isActive={false}
                        onSelect={() => onSelectSession(session.id)}
                        onDelete={() => onDeleteSession(session.id)}
                      />
                    ))
                  )}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

export default TaskPanel;
