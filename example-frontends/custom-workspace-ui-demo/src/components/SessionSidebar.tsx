/**
 * SessionSidebar - Multi-session management sidebar.
 * 
 * Displays active sessions and resumable sessions from disk,
 * Multi-session management sidebar component.
 */

import React, { useState, useEffect, useMemo } from 'react';
import {
  useSessionStore,
  Session,
} from '../stores/sessionStore';

// ============================================================================
// Props & Types
// ============================================================================

interface SessionSidebarProps {
  onCreateSession: (cwd: string) => Promise<void>;
  onResumeSession: (sessionId: string) => Promise<void>;
  onDeleteSession: (sessionId: string) => Promise<void>;
  onRefreshResumable: () => Promise<void>;
  currentProjectPath: string;
}

// ============================================================================
// Status Indicator
// ============================================================================

function StatusIndicator({ status }: { status: Session['status'] }) {
  const colors: Record<Session['status'], string> = {
    idle: 'bg-green-500',
    working: 'bg-yellow-500 animate-pulse',
    awaiting_approval: 'bg-red-500 animate-pulse',
  };
  
  return (
    <span
      className={`inline-block w-2 h-2 rounded-full ${colors[status]}`}
      title={status}
    />
  );
}

// ============================================================================
// Session Item
// ============================================================================

interface SessionItemProps {
  session: Session;
  isActive: boolean;
  onClick: () => void;
  onDelete: () => void;
}

function SessionItem({ session, isActive, onClick, onDelete }: SessionItemProps) {
  const [showDelete, setShowDelete] = useState(false);
  
  // Extract directory name from cwd
  const dirName = session.cwd.split('/').pop() || session.cwd;
  
  // First message preview
  const firstMessage = session.messages.find((m) => m.role === 'user');
  const preview = firstMessage?.content.slice(0, 50) || 'New session';
  
  return (
    <div
      className={`
        group flex items-center gap-2 p-2 rounded-lg cursor-pointer
        transition-colors duration-150
        ${isActive 
          ? 'bg-blue-600 text-white' 
          : 'hover:bg-gray-700 text-gray-300'
        }
      `}
      onClick={onClick}
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
    >
      <StatusIndicator status={session.status} />
      
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium truncate text-sm">
            {session.title !== 'New Chat' ? session.title : dirName}
          </span>
        </div>
        <p className="text-xs text-gray-400 truncate">{preview}</p>
      </div>
      
      {showDelete && (
        <button
          className={`
            p-1 rounded hover:bg-red-500 transition-colors
            ${isActive ? 'text-white' : 'text-gray-400'}
          `}
          onClick={(e) => {
            e.stopPropagation();
            onDelete();
          }}
          title="Delete session"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>
      )}
    </div>
  );
}

// ============================================================================
// Resumable Session Item
// ============================================================================


// ============================================================================
// New Session Button
// ============================================================================

interface NewSessionButtonProps {
  onClick: () => void;
  disabled?: boolean;
}

function NewSessionButton({ onClick, disabled }: NewSessionButtonProps) {
  return (
    <button
      className={`
        w-full flex items-center justify-center gap-2 p-2 rounded-lg
        bg-blue-600 hover:bg-blue-700 text-white font-medium
        transition-colors duration-150
        ${disabled ? 'opacity-50 cursor-not-allowed' : ''}
      `}
      onClick={onClick}
      disabled={disabled}
    >
      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
      </svg>
      <span>New Session</span>
    </button>
  );
}

// ============================================================================
// Main Component
// ============================================================================

export function SessionSidebar({
  onCreateSession,
  onResumeSession,
  onDeleteSession,
  onRefreshResumable,
  currentProjectPath,
}: SessionSidebarProps) {
  // Use primitive selectors and derive sessions list with useMemo
  const sessionsMap = useSessionStore((s) => s.sessions);
  const sessions = useMemo(
    () =>
      Object.values(sessionsMap).sort(
        (a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime(),
      ),
    [sessionsMap],
  );
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const resumableSessions: never[] = [];
  const setActiveSession = useSessionStore((s) => s.setActiveSession);
  const wsConnected = true; // Always connected in v2 (SSE-based)
  
  const [isCreating, setIsCreating] = useState(false);
  const [showResumable, setShowResumable] = useState(true);
  
  // Refresh resumable sessions on mount only
  useEffect(() => {
    onRefreshResumable();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Run only on mount to prevent infinite loop
  
  const handleCreateSession = async () => {
    if (isCreating) return;
    setIsCreating(true);
    try {
      await onCreateSession(currentProjectPath);
    } finally {
      setIsCreating(false);
    }
  };
  
  const handleResumeSession = async (sessionId: string) => {
    await onResumeSession(sessionId);
  };
  
  
  return (
    <div className="flex flex-col h-full bg-gray-900 text-white">
      {/* Header */}
      <div className="p-4 border-b border-gray-700">
        <h2 className="text-lg font-semibold mb-3">Sessions</h2>
        <NewSessionButton onClick={handleCreateSession} disabled={isCreating} />
      </div>
      
      {/* Active Sessions */}
      <div className="flex-1 overflow-y-auto p-2">
        {sessions.length > 0 ? (
          <div className="space-y-1">
            {sessions.map((session) => (
              <SessionItem
                key={session.id}
                session={session}
                isActive={session.id === activeSessionId}
                onClick={() => setActiveSession(session.id)}
                onDelete={() => onDeleteSession(session.id)}
              />
            ))}
          </div>
        ) : (
          <p className="text-center text-gray-500 text-sm py-4">
            No active sessions
          </p>
        )}
        
      </div>
      
      {/* Footer - Connection Status */}
      <div className="p-3 border-t border-gray-700 text-xs text-gray-500">
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full ${
              wsConnected ? 'bg-green-500' : 'bg-red-500'
            }`}
          />
          <span>
            {wsConnected ? 'Connected' : 'Disconnected'}
          </span>
        </div>
      </div>
    </div>
  );
}

export default SessionSidebar;
