/**
 * TaskCard Component (Phase 2.1)
 *
 * Individual task card for the Kanban board.
 * Shows title, agent, elapsed time, progress, and quick actions.
 */

import React, { useState, useEffect } from 'react';
import { Sun, Sparkles, Terminal, Play, Pause, CheckCircle, Bot } from 'lucide-react';
import { ToolId, ChatSession, TaskStatus, isTaskSession } from '../types';
import { getTaskElapsedTime, getTaskStats } from '../services/taskStore';
import { formatTimeCompact } from '../services/todoParser';

// Agent icons and colors mapping
const AGENT_INFO: Record<ToolId, { icon: React.ReactNode; color: string; name: string }> = {
  claude: { icon: <Sun size={12} />, color: 'text-orange-500', name: 'Claude' },
  gemini: { icon: <Sparkles size={12} />, color: 'text-blue-400', name: 'Gemini' },
  codex: { icon: <Terminal size={12} />, color: 'text-purple-400', name: 'Codex' },
  agent: { icon: <Bot size={12} />, color: 'text-emerald-400', name: 'Agent' },
};

interface TaskCardProps {
  toolId: ToolId;
  session: ChatSession;
  onNavigate: () => void;
  onUpdateStatus: (status: TaskStatus) => void;
}

export const TaskCard: React.FC<TaskCardProps> = ({
  toolId,
  session,
  onNavigate,
  onUpdateStatus
}) => {
  const [isHovered, setIsHovered] = useState(false);
  const [elapsedTime, setElapsedTime] = useState(0);

  // Type guard check
  if (!isTaskSession(session)) return null;

  const task = session.task;
  const stats = getTaskStats(session);
  const agent = AGENT_INFO[toolId];

  // Update elapsed time for in-progress tasks
  useEffect(() => {
    if (task.status !== TaskStatus.IN_PROGRESS) {
      setElapsedTime(getTaskElapsedTime(session));
      return;
    }

    const updateTime = () => setElapsedTime(getTaskElapsedTime(session));
    updateTime();
    const interval = setInterval(updateTime, 1000);
    return () => clearInterval(interval);
  }, [session, task.status, task.lastResumedAt]);

  return (
    <div
      className="bg-ide-panel rounded-lg border border-ide-border hover:border-ide-accent/50
                 cursor-pointer transition-all duration-200 group"
      onClick={onNavigate}
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
    >
      {/* Card Content */}
      <div className="p-3">
        {/* Header: Title and Agent Badge */}
        <div className="flex items-start justify-between gap-2">
          <h3 className="font-medium text-sm text-ide-textLight line-clamp-2 flex-1">
            {session.title}
          </h3>
          {/* Agent Badge */}
          <div className={`flex items-center gap-1 px-1.5 py-0.5 rounded text-xs ${agent.color} bg-white/5 flex-shrink-0`}>
            {agent.icon}
          </div>
        </div>

        {/* Progress Bar */}
        {stats.totalItems > 0 && (
          <div className="mt-3">
            <div className="flex justify-between text-xs text-ide-text mb-1">
              <span>Progress</span>
              <span>{stats.completedItems}/{stats.totalItems}</span>
            </div>
            <div className="w-full h-1.5 bg-white/10 rounded-full overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-blue-500 to-purple-500 transition-all duration-300"
                style={{ width: `${stats.progress}%` }}
              />
            </div>
          </div>
        )}

        {/* Footer: Time & Quick Actions */}
        <div className="mt-3 flex items-center justify-between">
          <div className="text-xs text-ide-text font-mono">
            {formatTimeCompact(elapsedTime)}
          </div>

          {/* Quick Actions (show on hover) */}
          <div className={`flex items-center gap-1 transition-opacity ${isHovered ? 'opacity-100' : 'opacity-0'}`}>
            {task.status === TaskStatus.IN_PROGRESS && (
              <button
                onClick={(e) => { e.stopPropagation(); onUpdateStatus(TaskStatus.PAUSED); }}
                className="p-1 rounded hover:bg-yellow-500/20 text-yellow-400"
                title="Pause"
              >
                <Pause size={12} />
              </button>
            )}
            {task.status === TaskStatus.PAUSED && (
              <button
                onClick={(e) => { e.stopPropagation(); onUpdateStatus(TaskStatus.IN_PROGRESS); }}
                className="p-1 rounded hover:bg-blue-500/20 text-blue-400"
                title="Resume"
              >
                <Play size={12} />
              </button>
            )}
            {task.status !== TaskStatus.COMPLETED && (
              <button
                onClick={(e) => { e.stopPropagation(); onUpdateStatus(TaskStatus.COMPLETED); }}
                className="p-1 rounded hover:bg-green-500/20 text-green-400"
                title="Complete"
              >
                <CheckCircle size={12} />
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};

export default TaskCard;
