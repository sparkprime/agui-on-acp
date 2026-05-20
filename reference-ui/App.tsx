/**
 * ACP Agent Web App — AG-UI SSE stream architecture.
 *
 * Uses v2 API + session store + useAgUiStream hook for all AI chat interactions.
 * Side-channel features (files, git) use REST APIs directly.
 */

import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import ChatPanel from './components/ChatPanel';
import ConfigPanel from './components/ConfigPanel';
import ProjectSelector from './components/ProjectSelector';
import { Message, Sender, Tab, TaskStatus } from './types';
import {
  useSessionStore,
  createSessionFromTask,
  type MessageAttachment,
} from './stores/sessionStore';
import * as v2Api from './src/services/v2Api';
import { useAgUiStream } from './src/hooks/useAgUiStream';

const App: React.FC = () => {
  // Project directory
  const [projectDir, setProjectDir] = useState<string | null>(null);

  // Session store
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const addSession = useSessionStore((s) => s.addSession);
  const setActive = useSessionStore((s) => s.setActiveSession);
  const removeSession = useSessionStore((s) => s.removeSession);

  // AG-UI stream hook
  const { startRun, cancelRun, sendApproval, isStreaming } = useAgUiStream();

  // Derive active session
  const activeSession = useMemo(
    () => (activeSessionId ? sessions[activeSessionId] : undefined),
    [activeSessionId, sessions],
  );

  // UI state
  const [activeTab, setActiveTab] = useState<Tab>(Tab.PREVIEW);
  const [isConfigOpen, setIsConfigOpen] = useState(false);
  const [currentAgent, setCurrentAgent] = useState<string>('');
  const [isCreatingSession, setIsCreatingSession] = useState(false);

  // Default agent preference (persisted in localStorage)
  const [defaultAgentId, setDefaultAgentId] = useState(
    () => localStorage.getItem('acp-ui-default-agent') || 'default',
  );
  const handleDefaultAgentChange = useCallback((agentId: string) => {
    setDefaultAgentId(agentId);
    if (agentId) {
      localStorage.setItem('acp-ui-default-agent', agentId);
    } else {
      localStorage.removeItem('acp-ui-default-agent');
    }
  }, []);

  // ── Draggable divider state ────────────────────────────────────────────
  const [chatWidthPercent, setChatWidthPercent] = useState(55);
  const isDragging = useRef(false);
  const containerRef = useRef<HTMLDivElement>(null);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isDragging.current = true;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (!isDragging.current || !containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const percent = (x / rect.width) * 100;
      // Clamp between 25% and 75%
      setChatWidthPercent(Math.min(75, Math.max(25, percent)));
    };

    const handleMouseUp = () => {
      if (isDragging.current) {
        isDragging.current = false;
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      }
    };

    document.addEventListener('mousemove', handleMouseMove);
    document.addEventListener('mouseup', handleMouseUp);
    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
    };
  }, []);

  const handleDividerDoubleClick = useCallback(() => {
    setChatWidthPercent(50);
  }, []);

  // ── Load session message history ───────────────────────────────────────

  const loadSessionHistory = useCallback(
    async (taskId: string) => {
      try {
        const { messages: historyMsgs } = await v2Api.getMessages(taskId);
        if (historyMsgs.length === 0) return;

        const chatMessages = historyMsgs.map((m) => {
          if (m.role === 'tool') {
            return {
              role: 'tool' as const,
              content: m.toolPurpose || `Tool: ${m.toolName}`,
              timestamp: Date.now(),
              toolCall: {
                toolCallId: `history-${Math.random().toString(36).slice(2)}`,
                toolName: m.toolName || 'unknown',
                parameters: m.toolParameters || {},
                status: 'completed' as const,
              },
            };
          }
          return {
            role: m.role as 'user' | 'agent',
            content: m.content,
            timestamp: Date.now(),
          };
        });

        useSessionStore.getState().setMessages(taskId, chatMessages);
      } catch (err) {
        console.debug('[App] No history for task:', taskId, err);
      }
    },
    [],
  );

  // ── Task list on mount ─────────────────────────────────────────────────

  useEffect(() => {
    if (!projectDir) return;
    v2Api.listTasks().then(({ tasks }) => {
      for (const t of tasks) {
        if (!sessions[t.taskId]) {
          addSession(
            createSessionFromTask(t.taskId, t.agentSessionId, t.cwd, {
              title: t.title,
            }),
          );
        }
      }
    }).catch((err) => console.error('[App] Failed to list tasks:', err));
  }, [projectDir]);

  // ── Messages for ChatPanel ────────────────────────────────────────────

  const messages: Message[] = (activeSession?.messages || []).map((m, idx) => ({
    id: `${activeSession?.id}-${idx}`,
    text: m.content,
    sender: m.role === 'user' ? Sender.USER : Sender.AGENT,
    timestamp: m.timestamp,
    isError: m.isError,
  }));

  const sessionsList = useMemo(() => Object.values(sessions), [sessions]);

  // ── Session management ─────────────────────────────────────────────────

  const handleCreateTask = useCallback(
    async (cwd: string, resumeSessionId?: string) => {
      setIsCreatingSession(true);
      try {
        const resp = await v2Api.createTask(cwd, {
          resumeSessionId: resumeSessionId,
          mode: defaultAgentId || undefined,
        });
        const session = createSessionFromTask(
          resp.taskId,
          resp.agentSessionId,
          cwd,
          {
            modes: resp.modes,
            models: resp.models,
            currentModeId: resp.currentModeId,
          },
        );
        addSession(session);
        setActive(session.id);

        // If resuming, load message history
        if (resumeSessionId) {
          await loadSessionHistory(resp.taskId);
        }
      } catch (err) {
        console.error('[App] Failed to create task:', err);
      } finally {
        setIsCreatingSession(false);
      }
    },
    [addSession, setActive, loadSessionHistory, defaultAgentId],
  );

  const handleDeleteTask = useCallback(
    async (taskId: string) => {
      try {
        await v2Api.deleteTask(taskId);
        removeSession(taskId);
      } catch (err) {
        console.error('[App] Failed to delete task:', err);
      }
    },
    [removeSession],
  );

  // ── Chat ───────────────────────────────────────────────────────────────

  const handleSendMessage = useCallback(
    async (text: string, attachments?: MessageAttachment[]) => {
      if (!activeSession) return;
      try {
        await startRun(activeSession.id, text, attachments);
      } catch (err) {
        console.error('[App] Failed to send:', err);
      }
    },
    [activeSession, startRun],
  );

  // ── Misc ───────────────────────────────────────────────────────────────

  const handleAgentChange = (agent: string) => {
    if (activeSession && agent) {
      v2Api.setMode(activeSession.id, agent).catch(console.error);
      useSessionStore.getState().updateSession(activeSession.id, { currentModeId: agent });
    }
    setCurrentAgent(agent);
  };

  const handleModelChange = (modelId: string) => {
    if (activeSession && modelId) {
      v2Api.setModel(activeSession.id, modelId).catch(console.error);
      useSessionStore.getState().updateSession(activeSession.id, { model: modelId });
    }
  };

  // ── Render ─────────────────────────────────────────────────────────────

  if (!projectDir) {
    return <ProjectSelector onProjectSelect={setProjectDir} />;
  }

  const chatSessions = sessionsList.map((s) => ({
    id: s.id,
    title: s.title,
    messages: s.messages.map((m, idx) => ({
      id: `${s.id}-${idx}`,
      text: m.content,
      sender: m.role === 'user' ? Sender.USER : Sender.AGENT,
      timestamp: m.timestamp,
    })),
    createdAt: new Date(s.createdAt).getTime(),
    updatedAt: new Date(s.updatedAt).getTime(),
    toolId: 'agent' as const,
  }));

  const legacyActiveSession = activeSession
    ? {
        id: activeSession.id,
        title: activeSession.title,
        messages,
        createdAt: new Date(activeSession.createdAt).getTime(),
        updatedAt: new Date(activeSession.updatedAt).getTime(),
        toolId: 'agent' as const,
      }
    : undefined;

  return (
    <div className="flex w-screen h-screen bg-ide-bg text-ide-text font-sans selection:bg-orange-500/30 p-sl-inset">
      <div ref={containerRef} className="flex-1 flex overflow-hidden gap-sl-gap">
        {/* Chat Panel */}
        <div
          style={{ width: `${chatWidthPercent}%` }}
          className="flex-shrink-0 h-full rounded-sl-1 overflow-hidden border border-white/[0.08]"
        >
          <ChatPanel
            messages={messages}
            onSendMessage={handleSendMessage}
            isGenerating={isStreaming || activeSession?.status === 'working'}
            activeTool="agent"
            onOpenConfig={() => setIsConfigOpen(true)}
            currentAgent={currentAgent}
            onAgentChange={handleAgentChange}
            chatSessions={chatSessions}
            activeSessionId={activeSession?.id || null}
            onNewChat={() => !isCreatingSession && handleCreateTask(projectDir)}
            isCreatingSession={isCreatingSession}
            onModelChange={handleModelChange}
            onSwitchSession={(id) => {
              setActive(id);
              const sess = sessions[id];
              if (sess && sess.messages.length === 0) {
                loadSessionHistory(id);
              }
            }}
            onDeleteSession={handleDeleteTask}
            activeSession={legacyActiveSession}
            onPauseTask={() => {}}
            onResumeTask={() => {}}
            onCompleteTask={() => {}}
            onToggleTaskItem={() => {}}
            currentModel={activeSession?.model || 'auto'}
            currentMode={activeSession?.currentModeId || 'code'}
            projectDir={projectDir}
            onApproveToolCall={(sessionId, toolCallId, optionId) => {
              sendApproval(sessionId, toolCallId, true, optionId);
            }}
            onRejectToolCall={(sessionId, toolCallId) => {
              sendApproval(sessionId, toolCallId, false);
            }}
            onBranchFromMessage={async (content) => {
              if (!projectDir || isCreatingSession) return;
              setIsCreatingSession(true);
              try {
                const resp = await v2Api.createTask(projectDir, {
                mode: defaultAgentId || undefined,
              });
                const session = createSessionFromTask(
                  resp.taskId,
                  resp.agentSessionId,
                  projectDir,
                  {
                    modes: resp.modes,
                    models: resp.models,
                    currentModeId: resp.currentModeId,
                  },
                );
                addSession(session);
                setActive(session.id);
                // Send the branched message as the first message in the new session
                await startRun(resp.taskId, content);
              } catch (err) {
                console.error('[App] Failed to branch session:', err);
              } finally {
                setIsCreatingSession(false);
              }
            }}
          />
        </div>

        {/* Draggable Divider */}
        <div
          onMouseDown={handleMouseDown}
          onDoubleClick={handleDividerDoubleClick}
          className="flex-shrink-0 w-2 cursor-col-resize relative group z-50 flex items-center justify-center -mx-1"
        >
          <div className="w-1 h-12 rounded-full bg-white/10 group-hover:bg-ide-accent/60 group-active:bg-ide-accent transition-colors" />
        </div>

        {/* Right panel placeholder */}
        <div
          style={{ width: `${100 - chatWidthPercent}%` }}
          className="flex-shrink-0 h-full rounded-sl-1 overflow-hidden border border-white/[0.08] bg-ide-panel flex items-center justify-center"
        >
          <div className="text-center text-gray-600">
            <p className="text-sm">Workspace panel</p>
            <p className="text-xs mt-1">Files, Git, Tasks</p>
          </div>
        </div>
      </div>

      <ConfigPanel
        isOpen={isConfigOpen}
        onClose={() => setIsConfigOpen(false)}
        activeTool="agent"
        defaultAgentId={defaultAgentId}
        onDefaultAgentChange={handleDefaultAgentChange}
        availableAgents={activeSession?.modes || []}
      />
    </div>
  );
};

export default App;
