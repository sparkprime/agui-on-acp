import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { Send, Bot, User, Sparkles, Files, Sun, Terminal as TerminalIcon, SlidersHorizontal, Copy, Check, ChevronDown, ChevronUp, Users, Plus, MessageSquare, Trash2, History, Search, Download, X, File, Folder, AlertCircle, FolderOpen, Wrench, Brain, Loader2, Cpu, GitBranch, Paperclip, Image as ImageIcon } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { Terminal } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import '@xterm/xterm/css/xterm.css';
import { Message, Sender, ToolId, ChatSession, isTaskSession } from '../types';
import { TaskHeader } from './TaskHeader';
import { ToolCard } from './ToolCard';
import { ThinkingBlock } from './ThinkingBlock';
import { useSessionStore, ChatMessage, ToolCall, MessageAttachment } from '../stores/sessionStore';

import { fetchPathSuggestions, PathSuggestion } from '../services/api';
import { slashCommands } from '../services/slashCommands';


// Terminal output component using xterm (for chat message display)
const TerminalMessage: React.FC<{ content: string }> = ({ content }) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const xtermRef = useRef<Terminal | null>(null);

  useEffect(() => {
    if (!terminalRef.current || xtermRef.current) return;

    const term = new Terminal({
      theme: {
        background: '#000000',
        foreground: '#e0e0e0',
        cursor: 'transparent',
        cursorAccent: 'transparent',
        selectionBackground: '#FF6B00',
      },
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
      fontSize: 12,
      lineHeight: 1.2,
      cursorBlink: false,
      disableStdin: true,
      convertEol: true,
      scrollback: 0,
    });

    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);

    term.open(terminalRef.current);
    term.write(content);

    const lines = content.split('\n').length;
    term.resize(80, Math.max(lines + 1, 3));

    xtermRef.current = term;

    return () => {
      term.dispose();
      xtermRef.current = null;
    };
  }, [content]);

  return (
    <div
      ref={terminalRef}
      className="rounded-lg overflow-hidden border border-white/5 bg-black"
      style={{ minHeight: '60px', maxHeight: '300px' }}
    />
  );
};

// Code block with copy button
const CodeBlock: React.FC<{ language?: string; children: string }> = ({ language, children }) => {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    await navigator.clipboard.writeText(children);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const isCommand = language === 'bash' || language === 'sh' || language === 'shell' || language === 'zsh';
  const isHtml = language === 'html';

  if (isHtml && children.length > 200) {
    return (
      <div className="my-2 p-2 bg-black/30 rounded border border-white/5 text-xs font-mono text-orange-400 flex items-center gap-2">
        <Files size={14} />
        <span>Code generated. Check preview.</span>
      </div>
    );
  }

  return (
    <div className="my-2 relative group">
      <div className="flex items-center justify-between bg-[#111] px-3 py-1.5 rounded-t border border-white/5 border-b-0">
        <span className="text-[10px] text-gray-500 uppercase font-medium">
          {isCommand ? 'Command' : language || 'code'}
        </span>
        <button
          onClick={handleCopy}
          className="flex items-center gap-1.5 text-[10px] text-gray-500 hover:text-white transition-colors px-2 py-0.5 rounded hover:bg-white/5"
        >
          {copied ? (
            <>
              <Check size={12} className="text-green-400" />
              <span className="text-green-400">Copied!</span>
            </>
          ) : (
            <>
              <Copy size={12} />
              <span>Copy</span>
            </>
          )}
        </button>
      </div>
      <pre className={`p-3 bg-black/60 rounded-b border border-white/5 border-t-0 overflow-x-auto ${
        isCommand ? 'text-orange-300' : 'text-gray-300'
      }`}>
        <code className="text-xs font-mono">{children}</code>
      </pre>
    </div>
  );
};

interface ChatPanelProps {
  messages: Message[];
  onSendMessage: (text: string, attachments?: MessageAttachment[]) => void;
  isGenerating: boolean;
  activeTool: ToolId;
  onOpenConfig: () => void;
  currentAgent: string;
  onAgentChange: (agent: string) => void;
  chatSessions: ChatSession[];
  activeSessionId: string | null;
  onNewChat: () => void;
  onSwitchSession: (sessionId: string) => void;
  onDeleteSession: (sessionId: string) => void;
  activeSession?: ChatSession;
  onPauseTask?: () => void;
  onResumeTask?: () => void;
  onCompleteTask?: () => void;
  onToggleTaskItem?: (itemId: string) => void;
  currentModel?: string;
  currentMode?: string;
  projectDir?: string;
  onApproveToolCall?: (sessionId: string, toolCallId: string, optionId: string) => void;
  onRejectToolCall?: (sessionId: string, toolCallId: string) => void;
  isCreatingSession?: boolean;
  onModelChange?: (modelId: string) => void;
  onBranchFromMessage?: (content: string) => void;
  
}

// ============================================================================
// Pending Approvals Banner Component
// ============================================================================

interface PendingApprovalsBannerProps {
  pendingApprovals: ToolCall[];
  sessionId: string;
  onApprove?: (sessionId: string, toolCallId: string, optionId: string) => void;
  onReject?: (sessionId: string, toolCallId: string) => void;
}

const KIND_STYLE: Record<string, string> = {
  allow_once: 'bg-green-600 hover:bg-green-500',
  allow_always: 'bg-orange-600 hover:bg-orange-500',
  reject_once: 'bg-red-600 hover:bg-red-500',
  reject_always: 'bg-red-800 hover:bg-red-700',
};

const PendingApprovalsBanner: React.FC<PendingApprovalsBannerProps> = ({
  pendingApprovals,
  sessionId,
  onApprove,
  onReject,
}) => {
  if (pendingApprovals.length === 0) return null;

  const handleApprove = (toolCallId: string, optionId: string) => {
    onApprove?.(sessionId, toolCallId, optionId);
  };

  return (
    <div className="border-t border-orange-500/20 bg-orange-500/5">
      <div className="px-4 py-2">
        <div className="flex items-center gap-2 mb-2">
          <AlertCircle size={16} className="text-orange-400" />
          <span className="text-sm font-medium text-orange-400">
            Tool{pendingApprovals.length > 1 ? 's' : ''} Require Approval ({pendingApprovals.length})
          </span>
        </div>
        
        <div className="space-y-3">
          {pendingApprovals.map((pending) => (
            <div
              key={pending.toolCallId}
              className="bg-ide-bg/50 rounded-lg p-3 border border-orange-500/15"
            >
              <div className="flex items-center gap-2 mb-2">
                <Wrench size={14} className="text-gray-400" />
                <span className="font-medium text-sm text-white">
                  {pending.toolName}
                </span>
                {pending.purpose && (
                  <span className="text-xs text-gray-500 truncate flex-1">
                    — {pending.purpose}
                  </span>
                )}
              </div>
              
              <div className="flex flex-wrap gap-2">
                {pending.permissionOptions && pending.permissionOptions.length > 0 ? (
                  pending.permissionOptions.map((opt) => (
                    <button
                      key={opt.id}
                      onClick={() => handleApprove(pending.toolCallId, opt.id)}
                      className={`px-3 py-1.5 text-xs font-medium text-white rounded transition-colors ${
                        KIND_STYLE[opt.kind ?? ''] ?? 'bg-gray-600 hover:bg-gray-500'
                      }`}
                      title={opt.description}
                    >
                      {opt.label}
                    </button>
                  ))
                ) : (
                  <>
                    <button
                      onClick={() => handleApprove(pending.toolCallId, 'allow_once')}
                      className="px-3 py-1.5 text-xs font-medium text-white rounded bg-green-600 hover:bg-green-500 transition-colors"
                    >
                      Approve
                    </button>
                    <button
                      onClick={() => handleApprove(pending.toolCallId, 'reject_once')}
                      className="px-3 py-1.5 text-xs font-medium text-white rounded bg-red-600 hover:bg-red-500 transition-colors"
                    >
                      Reject
                    </button>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
};

// Helper to detect @path in input
function extractAtPath(text: string, cursorPos: number): { path: string; startIdx: number } | null {
  let idx = cursorPos - 1;
  while (idx >= 0 && text[idx] !== '@' && text[idx] !== ' ' && text[idx] !== '\n') {
    idx--;
  }
  if (idx >= 0 && text[idx] === '@') {
    return {
      path: text.slice(idx + 1, cursorPos),
      startIdx: idx,
    };
  }
  return null;
}

// Helper to detect #agent in input
function extractHashAgent(text: string, cursorPos: number): { query: string; startIdx: number } | null {
  let idx = cursorPos - 1;
  while (idx >= 0 && text[idx] !== '#' && text[idx] !== ' ' && text[idx] !== '\n') {
    idx--;
  }
  if (idx >= 0 && text[idx] === '#') {
    return {
      query: text.slice(idx + 1, cursorPos),
      startIdx: idx,
    };
  }
  return null;
}

// Helper to detect /command at start of input
function extractSlashCommand(text: string, cursorPos: number): { query: string; startIdx: number } | null {
  // Only trigger if / is at the beginning of the input (optionally with leading whitespace)
  const trimmedStart = text.search(/\S/);
  if (trimmedStart < 0) return null;
  if (text[trimmedStart] !== '/') return null;
  // Cursor must be within the first word (the command)
  const afterSlash = text.slice(trimmedStart + 1);
  const spaceIdx = afterSlash.search(/\s/);
  const commandEnd = spaceIdx >= 0 ? trimmedStart + 1 + spaceIdx : text.length;
  if (cursorPos > commandEnd) return null;
  return {
    query: text.slice(trimmedStart + 1, cursorPos).toLowerCase(),
    startIdx: trimmedStart,
  };
}

// Agent suggestion type
interface AgentSuggestion {
  id: string;
  name: string;
  description?: string;
}

// Command suggestion type
interface CommandSuggestion {
  name: string;
  description: string;
  source: 'builtin' | 'session';
}

// Motivational texts for empty session state carousel
const motivationalTexts = [
  "Your agent workspace starts here",
  "Connect any ACP agent, build any UI",
  "A reference implementation — make it yours",
  "Swap agents with one config line",
];

const ChatPanel: React.FC<ChatPanelProps> = ({
  messages,
  onSendMessage,
  isGenerating,
  activeTool,
  onOpenConfig,
  currentAgent,
  onAgentChange,
  chatSessions,
  activeSessionId,
  onNewChat,
  onSwitchSession,
  onDeleteSession,
  activeSession,
  onPauseTask,
  onResumeTask,
  onCompleteTask,
  onToggleTaskItem,
  currentModel,
  currentMode,
  projectDir,
  onApproveToolCall,
  onRejectToolCall,
  isCreatingSession,
  onModelChange,
  onBranchFromMessage,
}) => {
  const [inputValue, setInputValue] = useState('');
  const [showAgentDropdown, setShowAgentDropdown] = useState(false);
  const [showHistoryDropdown, setShowHistoryDropdown] = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const agentDropdownRef = useRef<HTMLDivElement>(null);
  const historyDropdownRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // File/image attachment state
  const [pendingAttachments, setPendingAttachments] = useState<MessageAttachment[]>([]);
  const [isDragOver, setIsDragOver] = useState(false);

  const [pathSuggestions, setPathSuggestions] = useState<PathSuggestion[]>([]);
  const [showPathDropdown, setShowPathDropdown] = useState(false);
  const [selectedPathIndex, setSelectedPathIndex] = useState(0);
  const [atPathInfo, setAtPathInfo] = useState<{ path: string; startIdx: number } | null>(null);

  // # agent autocomplete state
  const [agentSuggestions, setAgentSuggestions] = useState<AgentSuggestion[]>([]);
  const [showAgentAutocomplete, setShowAgentAutocomplete] = useState(false);
  const [selectedAgentIndex, setSelectedAgentIndex] = useState(0);
  const [hashAgentInfo, setHashAgentInfo] = useState<{ query: string; startIdx: number } | null>(null);

  // / command autocomplete state
  const [commandSuggestions, setCommandSuggestions] = useState<CommandSuggestion[]>([]);
  const [showCommandDropdown, setShowCommandDropdown] = useState(false);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [slashCommandInfo, setSlashCommandInfo] = useState<{ query: string; startIdx: number } | null>(null);

  // Message action state (copy feedback)
  const [copiedMsgIdx, setCopiedMsgIdx] = useState<number | null>(null);

  // Empty state text carousel
  const [textIndex, setTextIndex] = useState(0);
  const [isTextExiting, setIsTextExiting] = useState(false);

  // Footer model/agent selector state
  const [showFooterModelDropdown, setShowFooterModelDropdown] = useState(false);
  const [showFooterAgentDropdown, setShowFooterAgentDropdown] = useState(false);
  const footerModelRef = useRef<HTMLDivElement>(null);
  const footerAgentRef = useRef<HTMLDivElement>(null);

  const activeStoreSession = useSessionStore((s) => 
    activeSessionId ? s.sessions[activeSessionId] : undefined
  );
  const pendingApprovals = activeStoreSession?.pendingApprovals || [];

  // Derive the display model name from session models list
  const displayModel = useMemo(() => {
    if (!activeStoreSession) return currentModel || 'auto';
    if (activeStoreSession.models && activeStoreSession.models.length > 0) {
      const modelId = activeStoreSession.model || '';
      const found = activeStoreSession.models.find(m => m.id === modelId);
      if (found) return found.name;
      return activeStoreSession.models[0].name;
    }
    return currentModel || 'auto';
  }, [activeStoreSession, currentModel]);

  // Derive the display mode/agent name from session modes list
  const displayAgent = useMemo(() => {
    if (!activeStoreSession) return currentMode || 'code';
    if (activeStoreSession.modes && activeStoreSession.modes.length > 0) {
      const modeId = activeStoreSession.currentModeId || '';
      const found = activeStoreSession.modes.find(m => m.id === modeId);
      if (found) return found.name;
    }
    return currentMode || 'code';
  }, [activeStoreSession, currentMode]);

  // Derive the current agent's description from session modes
  const currentAgentDescription = useMemo(() => {
    if (!activeStoreSession?.modes) return undefined;
    const modeId = activeStoreSession.currentModeId || '';
    const found = activeStoreSession.modes.find(m => m.id === modeId);
    return found?.description;
  }, [activeStoreSession]);

  // Derive the agent model
  const agentModel = useMemo(() => {
    return undefined;
  }, []);

  const filteredSessions = useMemo(() => {
    if (!searchQuery.trim()) return chatSessions;
    const lowerQuery = searchQuery.toLowerCase();
    return chatSessions.filter(session => {
      if (session.title.toLowerCase().includes(lowerQuery)) return true;
      return session.messages.some(msg => msg.text.toLowerCase().includes(lowerQuery));
    });
  }, [chatSessions, searchQuery]);

  const handleExportSession = async (session: ChatSession, e: React.MouseEvent) => {
    e.stopPropagation();
    const lines = [`# ${session.title}\n`];
    for (const msg of session.messages) {
      const role = msg.sender === Sender.USER ? 'User' : 'Assistant';
      lines.push(`## ${role}\n\n${msg.text}\n`);
    }
    const markdown = lines.join('\n');
    const blob = new Blob([markdown], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${session.title.replace(/[^a-zA-Z0-9]/g, '_')}_${new Date(session.createdAt).toISOString().split('T')[0]}.md`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  // Text carousel rotation effect
  useEffect(() => {
    const interval = setInterval(() => {
      setIsTextExiting(true);
      setTimeout(() => {
        setTextIndex((prev) => (prev + 1) % motivationalTexts.length);
        setIsTextExiting(false);
      }, 250); // matches fade-out duration
    }, 2500);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    if (showHistoryDropdown && searchInputRef.current) {
      setTimeout(() => searchInputRef.current?.focus(), 100);
    }
    if (!showHistoryDropdown) setSearchQuery('');
  }, [showHistoryDropdown]);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => { scrollToBottom(); }, [messages]);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (agentDropdownRef.current && !agentDropdownRef.current.contains(event.target as Node)) {
        setShowAgentDropdown(false);
      }
      if (historyDropdownRef.current && !historyDropdownRef.current.contains(event.target as Node)) {
        setShowHistoryDropdown(false);
      }
      if (footerModelRef.current && !footerModelRef.current.contains(event.target as Node)) {
        setShowFooterModelDropdown(false);
      }
      if (footerAgentRef.current && !footerAgentRef.current.contains(event.target as Node)) {
        setShowFooterAgentDropdown(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  useEffect(() => {
    if (!atPathInfo || !projectDir) {
      setPathSuggestions([]);
      setShowPathDropdown(false);
      return;
    }
    const timeout = setTimeout(async () => {
      try {
        const suggestions = await fetchPathSuggestions(atPathInfo.path, projectDir);
        setPathSuggestions(suggestions);
        setShowPathDropdown(suggestions.length > 0);
        setSelectedPathIndex(0);
      } catch {
        setPathSuggestions([]);
        setShowPathDropdown(false);
      }
    }, 150);
    return () => clearTimeout(timeout);
  }, [atPathInfo?.path, projectDir]);

  // # agent autocomplete effect
  useEffect(() => {
    if (!hashAgentInfo) {
      setAgentSuggestions([]);
      setShowAgentAutocomplete(false);
      return;
    }
    const modes = activeStoreSession?.modes || [];
    const query = hashAgentInfo.query.toLowerCase();
    const filtered = modes
      .filter((m) => m.name.toLowerCase().includes(query) || m.id.toLowerCase().includes(query))
      .slice(0, 10)
      .map((m) => ({ id: m.id, name: m.name, description: (m as { description?: string }).description }));
    setAgentSuggestions(filtered);
    setShowAgentAutocomplete(filtered.length > 0);
    setSelectedAgentIndex(0);
  }, [hashAgentInfo?.query, activeStoreSession?.modes]);

  // / command autocomplete effect
  useEffect(() => {
    if (!slashCommandInfo) {
      setCommandSuggestions([]);
      setShowCommandDropdown(false);
      return;
    }
    const query = slashCommandInfo.query.toLowerCase();
    const suggestions: CommandSuggestion[] = [];

    // Built-in slash commands
    for (const cmd of Object.values(slashCommands)) {
      if (cmd.name.startsWith(query) || cmd.aliases?.some((a) => a.startsWith(query))) {
        suggestions.push({ name: cmd.name, description: cmd.description, source: 'builtin' });
      }
    }

    // Session available commands
    const sessionCmds = activeStoreSession?.availableCommands || [];
    for (const cmd of sessionCmds) {
      if (cmd.name.toLowerCase().startsWith(query)) {
        suggestions.push({ name: cmd.name, description: cmd.description || '', source: 'session' });
      }
    }

    setCommandSuggestions(suggestions.slice(0, 10));
    setShowCommandDropdown(suggestions.length > 0);
    setSelectedCommandIndex(0);
  }, [slashCommandInfo?.query, activeStoreSession?.availableCommands]);

  // ── File/Image attachment helpers ──────────────────────────────────────

  const IMAGE_MIME_TYPES = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'];
  const TEXT_EXTENSIONS = ['.txt','.md','.py','.js','.ts','.tsx','.jsx','.json','.yaml','.yml','.toml','.css','.html','.xml','.csv','.log','.sh','.bash','.rs','.go','.java','.c','.cpp','.h','.hpp','.rb','.swift','.kt','.sql','.env','.gitignore','.dockerfile'];
  const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB

  const isImageFile = (file: File) => IMAGE_MIME_TYPES.includes(file.type);
  const isTextFile = (file: File) => {
    const ext = '.' + file.name.split('.').pop()?.toLowerCase();
    return TEXT_EXTENSIONS.includes(ext) || file.type.startsWith('text/');
  };

  const readFileAsBase64 = (file: File): Promise<string> => {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = reader.result as string;
        // Strip data URL prefix to get raw base64
        const base64 = result.includes(',') ? result.split(',')[1] : result;
        resolve(base64);
      };
      reader.onerror = reject;
      reader.readAsDataURL(file);
    });
  };

  const addFilesAsAttachments = useCallback(async (files: FileList | File[]) => {
    const fileArray = Array.from(files);
    const newAttachments: MessageAttachment[] = [];

    for (const file of fileArray) {
      if (file.size > MAX_FILE_SIZE) {
        console.warn(`File ${file.name} exceeds 10MB limit, skipping`);
        continue;
      }
      if (!isImageFile(file) && !isTextFile(file)) {
        console.warn(`File ${file.name} is not a supported type, skipping`);
        continue;
      }

      const id = `att-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      const type = isImageFile(file) ? 'image' as const : 'file' as const;
      const base64 = await readFileAsBase64(file);
      const previewUrl = type === 'image' ? URL.createObjectURL(file) : undefined;

      newAttachments.push({
        id,
        name: file.name,
        type,
        mimeType: file.type || 'application/octet-stream',
        size: file.size,
        previewUrl,
        base64,
      });
    }

    if (newAttachments.length > 0) {
      setPendingAttachments((prev) => [...prev, ...newAttachments]);
    }
  }, []);

  const removeAttachment = useCallback((id: string) => {
    setPendingAttachments((prev) => {
      const att = prev.find((a) => a.id === id);
      if (att?.previewUrl) URL.revokeObjectURL(att.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  // Clean up preview URLs on unmount
  useEffect(() => {
    return () => {
      pendingAttachments.forEach((a) => {
        if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
      });
    };
  }, []);

  // ── Drag and drop handlers ────────────────────────────────────────────

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (activeSessionId) setIsDragOver(true);
  }, [activeSessionId]);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragOver(false);
    if (!activeSessionId) return;
    const files = e.dataTransfer.files;
    if (files.length > 0) addFilesAsAttachments(files);
  }, [activeSessionId, addFilesAsAttachments]);

  // ── Paste handler (for images from clipboard) ─────────────────────────

  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    if (!activeSessionId) return;
    const items = e.clipboardData?.items;
    if (!items) return;

    const imageFiles: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile();
        if (file) imageFiles.push(file);
      }
    }

    if (imageFiles.length > 0) {
      e.preventDefault(); // prevent pasting the image as text
      addFilesAsAttachments(imageFiles);
    }
  }, [activeSessionId, addFilesAsAttachments]);

  // ── File input handler ────────────────────────────────────────────────

  const handleFileInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (files && files.length > 0) {
      addFilesAsAttachments(files);
    }
    // Reset input so same file can be selected again
    e.target.value = '';
  }, [addFilesAsAttachments]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedInput = inputValue.trim();
    const hasAttachments = pendingAttachments.length > 0;
    if ((trimmedInput || hasAttachments) && !isGenerating) {
      onSendMessage(trimmedInput || (hasAttachments ? `[${pendingAttachments.length} file(s) attached]` : ''), hasAttachments ? pendingAttachments : undefined);
      setInputValue('');
      setPendingAttachments([]);
      setShowPathDropdown(false);
      setAtPathInfo(null);
    }
  };

  const completePath = useCallback((suggestion: PathSuggestion) => {
    if (!atPathInfo) return;
    const before = inputValue.slice(0, atPathInfo.startIdx);
    const after = inputValue.slice(atPathInfo.startIdx + 1 + atPathInfo.path.length);
    const newPath = suggestion.isDirectory ? `@${suggestion.path}/` : `@${suggestion.path} `;
    const newValue = before + newPath + after;
    setInputValue(newValue);
    setShowPathDropdown(false);
    setAtPathInfo(null);
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.focus();
        const newPos = before.length + newPath.length;
        textareaRef.current.setSelectionRange(newPos, newPos);
      }
    }, 0);
  }, [atPathInfo, inputValue]);

  const completeAgent = useCallback((suggestion: AgentSuggestion) => {
    if (!hashAgentInfo) return;
    const before = inputValue.slice(0, hashAgentInfo.startIdx);
    const after = inputValue.slice(hashAgentInfo.startIdx + 1 + hashAgentInfo.query.length);
    const replacement = `#${suggestion.name} `;
    const newValue = before + replacement + after;
    setInputValue(newValue);
    setShowAgentAutocomplete(false);
    setHashAgentInfo(null);
    // Also switch the agent mode
    onAgentChange(suggestion.id);
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.focus();
        const newPos = before.length + replacement.length;
        textareaRef.current.setSelectionRange(newPos, newPos);
      }
    }, 0);
  }, [hashAgentInfo, inputValue, onAgentChange]);

  const completeCommand = useCallback((suggestion: CommandSuggestion) => {
    if (!slashCommandInfo) return;
    const newValue = `/${suggestion.name} `;
    setInputValue(newValue);
    setShowCommandDropdown(false);
    setSlashCommandInfo(null);
    setTimeout(() => {
      if (textareaRef.current) {
        textareaRef.current.focus();
        textareaRef.current.setSelectionRange(newValue.length, newValue.length);
      }
    }, 0);
  }, [slashCommandInfo]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // @ file path dropdown navigation
    if (showPathDropdown && pathSuggestions.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedPathIndex((prev) => (prev + 1) % pathSuggestions.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedPathIndex((prev) => (prev - 1 + pathSuggestions.length) % pathSuggestions.length);
        return;
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        completePath(pathSuggestions[selectedPathIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setShowPathDropdown(false);
        return;
      }
    }
    // # agent dropdown navigation
    if (showAgentAutocomplete && agentSuggestions.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedAgentIndex((prev) => (prev + 1) % agentSuggestions.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedAgentIndex((prev) => (prev - 1 + agentSuggestions.length) % agentSuggestions.length);
        return;
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        completeAgent(agentSuggestions[selectedAgentIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setShowAgentAutocomplete(false);
        return;
      }
    }
    // / command dropdown navigation
    if (showCommandDropdown && commandSuggestions.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSelectedCommandIndex((prev) => (prev + 1) % commandSuggestions.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSelectedCommandIndex((prev) => (prev - 1 + commandSuggestions.length) % commandSuggestions.length);
        return;
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault();
        completeCommand(commandSuggestions[selectedCommandIndex]);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setShowCommandDropdown(false);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e as unknown as React.FormEvent);
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const newValue = e.target.value;
    setInputValue(newValue);
    const cursorPos = e.target.selectionStart || 0;

    // Detect @ file path trigger
    const pathInfo = extractAtPath(newValue, cursorPos);
    setAtPathInfo(pathInfo);

    // Detect # agent trigger
    const agentInfo = extractHashAgent(newValue, cursorPos);
    setHashAgentInfo(agentInfo);

    // Detect / command trigger
    const cmdInfo = extractSlashCommand(newValue, cursorPos);
    setSlashCommandInfo(cmdInfo);

    // Dismiss other dropdowns when one is active
    if (pathInfo) {
      setShowAgentAutocomplete(false);
      setShowCommandDropdown(false);
    } else if (agentInfo) {
      setShowPathDropdown(false);
      setShowCommandDropdown(false);
    } else if (cmdInfo) {
      setShowPathDropdown(false);
      setShowAgentAutocomplete(false);
    }
  };

  const getToolDetails = (id: ToolId) => {
    switch(id) {
      case 'claude': return { name: 'Claude Code', icon: Sun, color: 'text-orange-500' };
      case 'gemini': return { name: 'Gemini CLI', icon: Sparkles, color: 'text-white' };
      case 'codex': return { name: 'Codex CLI', icon: TerminalIcon, color: 'text-white' };
      case 'agent': return { name: 'Your Agent', icon: Bot, color: 'text-orange-500' };
      default: return { name: 'Your Agent', icon: Bot, color: 'text-gray-400' };
    }
  };

  const toolInfo = getToolDetails(activeTool);
  const ToolIcon = toolInfo.icon;

  return (
    <div className="flex flex-col h-full w-full bg-ide-panel">
      {/* Header — 3-column: [Left: Tool + ⚙] [Center: Agent] [Right: + History] */}
      <div className="h-12 border-b border-white/[0.06] flex items-center px-4 bg-ide-bg/80">
        {/* Left */}
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="font-semibold text-sm text-white flex items-center gap-2">
            <ToolIcon size={16} className={toolInfo.color} />
            {toolInfo.name}
          </span>
          <button
            onClick={onOpenConfig}
            className="p-1 rounded-md hover:bg-white/5 text-gray-500 hover:text-white transition-colors"
            title="Settings"
          >
            <SlidersHorizontal size={13} />
          </button>
        </div>

        {/* Center — Prominent Agent Selector */}
        <div className="flex-1 flex justify-center" ref={agentDropdownRef}>
          {activeSessionId ? (
            <div className="relative">
              <button
                onClick={() => {
                  if (activeStoreSession?.modes && activeStoreSession.modes.length > 0) {
                    setShowAgentDropdown(!showAgentDropdown);
                  }
                }}
                className={`flex items-center gap-2 px-3 py-1.5 rounded-sl-3 border transition-colors ${
                  activeStoreSession?.modes && activeStoreSession.modes.length > 0
                    ? 'border-orange-500/30 hover:border-orange-500/50 hover:bg-orange-500/5 cursor-pointer'
                    : 'border-ide-border cursor-default'
                }`}
              >
                <Users size={14} className="text-orange-400 flex-shrink-0" />
                <div className="flex flex-col items-start min-w-0">
                  <span className="text-sm font-medium text-gray-200 max-w-[180px] truncate leading-tight">
                    {displayAgent}
                  </span>
                  {currentAgentDescription && (
                    <span className="text-[10px] text-gray-500 max-w-[180px] truncate leading-tight">
                      {currentAgentDescription}
                    </span>
                  )}
                </div>
                {activeStoreSession?.modes && activeStoreSession.modes.length > 0 && (
                  <ChevronDown size={12} className={`text-gray-500 transition-transform flex-shrink-0 ${showAgentDropdown ? 'rotate-180' : ''}`} />
                )}
              </button>

              {showAgentDropdown && activeStoreSession?.modes && activeStoreSession.modes.length > 0 && (
                <div className="absolute left-1/2 -translate-x-1/2 top-full mt-1 w-72 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 overflow-hidden max-h-80 overflow-y-auto">
                  <div className="p-2 border-b border-ide-border sticky top-0 bg-[#111]">
                    <span className="text-[10px] text-gray-600 uppercase tracking-wider">Available Agents ({activeStoreSession.modes.length})</span>
                  </div>
                  {activeStoreSession.modes.map((mode) => (
                    <button
                      key={mode.id}
                      onClick={() => {
                        onAgentChange(mode.id);
                        setShowAgentDropdown(false);
                      }}
                      className={`w-full px-3 py-2 text-left hover:bg-white/5 transition-colors flex flex-col ${
                        activeStoreSession.currentModeId === mode.id
                          ? 'bg-orange-500/10 border-l-2 border-orange-500'
                          : ''
                      }`}
                    >
                      <span className="text-sm text-gray-200">{mode.name}</span>
                      {(mode as { description?: string }).description && (
                        <span className="text-[10px] text-gray-500 line-clamp-2">{(mode as { description?: string }).description}</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <span className="text-sm text-gray-600">No session</span>
          )}
        </div>

        {/* Right */}
        <div className="flex items-center gap-1.5 flex-shrink-0">
          {/* New Chat Button */}
          <button
            onClick={onNewChat}
            className="p-1.5 rounded-md hover:bg-white/5 text-gray-500 hover:text-white transition-colors"
            title="New Chat"
          >
            <Plus size={15} />
          </button>

          {/* Chat History Dropdown */}
          <div className="relative" ref={historyDropdownRef}>
            <button
              onClick={() => setShowHistoryDropdown(!showHistoryDropdown)}
              className="p-1.5 rounded-md hover:bg-white/5 text-gray-500 hover:text-white transition-colors"
              title="Chat History"
            >
              <History size={15} />
            </button>

            {showHistoryDropdown && (
              <div className="absolute right-0 top-full mt-1 w-72 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 overflow-hidden">
                <div className="p-2 border-b border-ide-border space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] text-gray-600 uppercase tracking-wider">Chat History</span>
                    <span className="text-[10px] text-gray-600">{chatSessions.length} chats</span>
                  </div>
                  <div className="relative">
                    <Search size={12} className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-600" />
                    <input
                      ref={searchInputRef}
                      type="text"
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      placeholder="Search chats..."
                      className="w-full bg-black border border-ide-border rounded pl-7 pr-7 py-1.5 text-xs text-gray-200 placeholder-gray-600 focus:outline-none focus:border-orange-500/50"
                    />
                    {searchQuery && (
                      <button
                        onClick={() => setSearchQuery('')}
                        className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-600 hover:text-gray-300"
                      >
                        <X size={12} />
                      </button>
                    )}
                  </div>
                </div>

                <div className="max-h-64 overflow-y-auto">
                  {filteredSessions.length === 0 ? (
                    <div className="p-4 text-center text-gray-600 text-xs">
                      {searchQuery ? 'No matching chats found' : 'No chat history'}
                    </div>
                  ) : (
                    filteredSessions.map((session) => (
                      <div
                        key={session.id}
                        className={`group flex items-center justify-between px-3 py-2 hover:bg-white/5 transition-colors cursor-pointer ${
                          session.id === activeSessionId ? 'bg-orange-500/10 border-l-2 border-orange-500' : ''
                        }`}
                        onClick={() => {
                          onSwitchSession(session.id);
                          setShowHistoryDropdown(false);
                        }}
                      >
                        <div className="flex items-center gap-2 flex-1 min-w-0">
                          <MessageSquare size={14} className="text-gray-600 flex-shrink-0" />
                          <div className="flex-1 min-w-0">
                            <span className="text-sm text-gray-300 block truncate">{session.title}</span>
                            <span className="text-[10px] text-gray-600">
                              {new Date(session.updatedAt).toLocaleDateString()}
                            </span>
                          </div>
                        </div>
                        <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-all">
                          <button
                            onClick={(e) => handleExportSession(session, e)}
                            className="p-1 rounded hover:bg-orange-500/10 text-gray-600 hover:text-orange-400"
                            title="Export as Markdown"
                          >
                            <Download size={12} />
                          </button>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              onDeleteSession(session.id);
                            }}
                            className="p-1 rounded hover:bg-red-500/10 text-gray-600 hover:text-red-400"
                            title="Delete chat"
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Pinned Project Directory */}
      {projectDir && (
        <div className="mx-2 mt-1 px-4 py-1.5 rounded-sl-3 bg-ide-bg/50 flex items-center gap-2 text-[11px] text-gray-600">
          <FolderOpen size={12} className="text-gray-500 flex-shrink-0" />
          <span className="truncate font-mono">{projectDir}</span>
        </div>
      )}

      {/* Task Header (if current session is a task) */}
      {activeSession && isTaskSession(activeSession) && (
        <TaskHeader
          session={activeSession}
          onPause={onPauseTask || (() => {})}
          onResume={onResumeTask || (() => {})}
          onComplete={onCompleteTask || (() => {})}
          onToggleItem={onToggleTaskItem || (() => {})}
        />
      )}

      {/* Messages — Level 2 rounded scroll area */}
      <div className="flex-1 overflow-y-auto m-2 space-y-6 rounded-sl-2">
        {/* Session creating state */}
        {isCreatingSession && (
          <div className="h-full flex flex-col items-center justify-center text-gray-600 space-y-4">
            <div className="w-16 h-16 rounded-full bg-ide-border flex items-center justify-center">
              <Loader2 size={32} className="text-orange-400 animate-spin" />
            </div>
            <p className="text-center text-sm text-gray-400">Creating session...</p>
            <p className="text-center text-xs text-gray-600">Initializing agent and MCP servers</p>
          </div>
        )}

        {/* No session selected state */}
        {!activeSessionId && !isCreatingSession && (
          <div className="h-full flex flex-col items-center justify-center text-gray-600 space-y-4">
            <div
              className="w-20 h-20 rounded-full bg-ide-border flex items-center justify-center"
              style={{
                animation: 'icon-float 3s ease-in-out infinite, icon-glow 4s ease-in-out infinite',
              }}
            >
              <Bot size={40} className="text-orange-500" />
            </div>
            <p
              className={`text-center text-base ${isTextExiting ? 'animate-text-fade-out' : 'animate-text-fade-in'}`}
              key={`no-session-${textIndex}`}
            >
              {motivationalTexts[textIndex]}
            </p>
            <button
              onClick={onNewChat}
              className="flex items-center gap-2 px-4 py-2.5 text-sm bg-orange-500 text-white rounded-lg hover:bg-orange-400 transition-colors"
            >
              <Plus size={16} />
              New Session
            </button>
          </div>
        )}
        
        {/* Empty session state */}
        {activeSessionId && messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-gray-600 space-y-4">
            <div
              className="w-20 h-20 rounded-full bg-ide-border flex items-center justify-center"
              style={{
                animation: 'icon-float 3s ease-in-out infinite, icon-glow 4s ease-in-out infinite',
              }}
            >
              <ToolIcon size={40} className={toolInfo.color} />
            </div>
            <p
              className={`text-center text-base ${isTextExiting ? 'animate-text-fade-out' : 'animate-text-fade-in'}`}
              key={textIndex}
            >
              {motivationalTexts[textIndex]}
            </p>

            {/* Agent details card */}
            {activeStoreSession && (
              <div className="mt-2 w-full max-w-sm px-4">
                <div className="bg-white/[0.03] border border-white/[0.06] rounded-xl p-4 space-y-3">
                  {/* Agent name & description */}
                  <div className="flex items-start gap-3">
                    <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-orange-500/10 flex-shrink-0 mt-0.5">
                      <Users size={16} className="text-orange-400" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <span className="text-sm font-medium text-white block">{displayAgent}</span>
                      {currentAgentDescription && (
                        <span className="text-xs text-gray-500 block mt-0.5 leading-relaxed">{currentAgentDescription}</span>
                      )}
                    </div>
                  </div>

                  {/* Details row: Model, MCP servers */}
                  <div className="flex flex-wrap gap-2 pt-1 border-t border-white/[0.04]">
                    {/* Model */}
                    <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] rounded-lg px-2.5 py-1.5">
                      <Cpu size={12} className="text-blue-400 flex-shrink-0" />
                      <span className="text-[11px] text-gray-400 font-mono">{agentModel || displayModel}</span>
                    </div>

                    {/* Mode ID */}
                    <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] rounded-lg px-2.5 py-1.5">
                      <Brain size={12} className="text-purple-400 flex-shrink-0" />
                      <span className="text-[11px] text-gray-400 font-mono">{activeStoreSession.currentModeId}</span>
                    </div>

                    {/* MCP Servers (from session) */}
                    {activeStoreSession.mcpServers.length > 0 && (
                      <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] rounded-lg px-2.5 py-1.5">
                        <Sparkles size={12} className="text-green-400 flex-shrink-0" />
                        <span className="text-[11px] text-gray-400">{activeStoreSession.mcpServers.length} MCP server{activeStoreSession.mcpServers.length !== 1 ? 's' : ''}</span>
                      </div>
                    )}

                    {/* Available agents count */}
                    {activeStoreSession.modes.length > 1 && (
                      <div className="flex items-center gap-1.5 bg-white/[0.03] border border-white/[0.06] rounded-lg px-2.5 py-1.5">
                        <Users size={12} className="text-orange-400 flex-shrink-0" />
                        <span className="text-[11px] text-gray-400">{activeStoreSession.modes.length} agents</span>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
        
        {/* Render messages */}
        {activeStoreSession?.messages.map((msg, idx) => {
          if (msg.role === 'tool' && msg.toolCall) {
            const isPending = pendingApprovals.some((p) => p.toolCallId === msg.toolCall?.toolCallId);
            return (
              <div key={`${activeSessionId}-${idx}`} className="flex justify-start">
                <div className="max-w-[90%]">
                  <ToolCard
                    toolCall={msg.toolCall}
                    sessionId={activeSessionId || ''}
                    isPending={isPending}
                  />
                </div>
              </div>
            );
          }

          // Render thinking messages with ThinkingBlock
          if (msg.role === 'agent' && msg.isThinking) {
            const isThinkingStreaming = isGenerating && !msg.thinkingDurationMs && idx === activeStoreSession.messages.length - 1;
            return (
              <div key={`${activeSessionId}-${idx}`} className="flex justify-start">
                <ThinkingBlock
                  content={msg.content}
                  durationMs={msg.thinkingDurationMs}
                  isStreaming={isThinkingStreaming}
                />
              </div>
            );
          }

          const isUser = msg.role === 'user';
          const isCopied = copiedMsgIdx === idx;
          return (
            <div key={`${activeSessionId}-${idx}`} className={`group/msg flex ${isUser ? 'justify-end' : 'justify-start'}`}>
              <div className={`relative ${isUser ? 'max-w-[90%]' : 'w-full'}`}>
                {/* Hover action bar — bottom, horizontal */}
                <div className="absolute bottom-0 translate-y-full pt-1 right-0 flex flex-row gap-1 opacity-0 group-hover/msg:opacity-100 transition-opacity">
                  <button
                    onClick={async () => {
                      await navigator.clipboard.writeText(msg.content);
                      setCopiedMsgIdx(idx);
                      setTimeout(() => setCopiedMsgIdx(null), 2000);
                    }}
                    className="flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-white/10 text-gray-600 hover:text-gray-300 transition-colors text-[10px]"
                    title="Copy message"
                  >
                    {isCopied ? <><Check size={11} className="text-green-400" /><span className="text-green-400">Copied</span></> : <><Copy size={11} /><span>Copy</span></>}
                  </button>
                  <button
                    onClick={() => onBranchFromMessage?.(msg.content)}
                    className="flex items-center gap-1 px-1.5 py-0.5 rounded hover:bg-white/10 text-gray-600 hover:text-gray-300 transition-colors text-[10px]"
                    title="Branch into new session"
                  >
                    <GitBranch size={11} /><span>Branch</span>
                  </button>
                </div>

                <div className={`rounded-2xl p-3 text-sm leading-relaxed ${
                  isUser
                    ? 'bg-orange-500/10 text-orange-100 rounded-tr-none border border-orange-500/10'
                    : 'text-gray-300'
                }`}>
                  {/* Attachment thumbnails (for user messages with attachments) */}
                  {msg.attachments && msg.attachments.length > 0 && (
                    <div className="flex flex-wrap gap-2 mb-2">
                      {msg.attachments.map((att) => (
                        <div key={att.id} className="flex items-center gap-1.5 bg-white/5 border border-white/10 rounded-lg overflow-hidden">
                          {att.type === 'image' && att.previewUrl ? (
                            <img
                              src={att.previewUrl}
                              alt={att.name}
                              className="h-16 max-w-[200px] object-cover rounded-lg cursor-pointer hover:opacity-80 transition-opacity"
                              onClick={() => window.open(att.previewUrl, '_blank')}
                            />
                          ) : (
                            <div className="flex items-center gap-2 px-2.5 py-1.5">
                              <File size={14} className="text-gray-500 flex-shrink-0" />
                              <span className="text-[11px] text-gray-300 truncate max-w-[120px]">{att.name}</span>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}

                  {!isUser ? (
                    <div className="prose prose-invert prose-sm max-w-none prose-p:my-2 prose-headings:mt-4 prose-headings:mb-2 prose-ul:my-2 prose-ol:my-2 prose-li:my-0.5 prose-hr:my-4 prose-blockquote:my-3 prose-pre:my-2 [&>p+ul]:mt-1 [&>p+ol]:mt-1">
                      <ReactMarkdown
                        components={{
                          code({node, className, children, ...props}) {
                            const match = /language-(\w+)/.exec(className || '');
                            const language = match ? match[1] : undefined;
                            const codeString = String(children).replace(/\n$/, '');
                            const isBlock = codeString.includes('\n') || language;

                            if (isBlock) {
                              return <CodeBlock language={language}>{codeString}</CodeBlock>;
                            }

                            return (
                              <code className="px-1.5 py-0.5 bg-white/5 rounded text-xs font-mono text-orange-300" {...props}>
                                {children}
                              </code>
                            );
                          }
                        }}
                      >
                        {msg.content}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    msg.content
                  )}
                </div>
              </div>
            </div>
          );
        })}
        
        {isGenerating && (() => {
          // Don't show the indicator if the last message is a thinking message (ThinkingBlock handles its own streaming state)
          const lastMsg = activeStoreSession?.messages[activeStoreSession.messages.length - 1];
          if (lastMsg?.role === 'agent' && lastMsg.isThinking) return null;
          // Don't show if last message is already an agent message being streamed
          if (lastMsg?.role === 'agent' && !lastMsg.isThinking) return null;
          return (
            <div className="flex justify-start">
              <div className="bg-white/[0.03] border border-white/5 rounded-2xl rounded-tl-none p-3 text-sm text-gray-500 flex items-center gap-2">
                <Sparkles size={14} className="text-orange-400 animate-pulse" />
                <span className="animate-pulse">Thinking<span className="tracking-widest">...</span></span>
              </div>
            </div>
          );
        })()}
        <div ref={messagesEndRef} />
      </div>

      {/* Pending Approvals Banner */}
      {activeSessionId && pendingApprovals.length > 0 && (
        <PendingApprovalsBanner
          pendingApprovals={pendingApprovals}
          sessionId={activeSessionId}
          onApprove={onApproveToolCall}
          onReject={onRejectToolCall}
        />
      )}

      {/* Input — unified container stretched to bottom */}
      <div className="mx-2 mb-2">
        <form onSubmit={handleSubmit} className="relative">
          {/* @ file path autocomplete dropdown */}
          {showPathDropdown && pathSuggestions.length > 0 && (
            <div className="absolute bottom-full left-0 right-0 mb-1 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 max-h-48 overflow-y-auto">
              <div className="px-3 py-1.5 border-b border-ide-border">
                <span className="text-[10px] text-gray-600 uppercase tracking-wider">Files</span>
              </div>
              {pathSuggestions.map((suggestion, idx) => (
                <button
                  key={suggestion.path}
                  type="button"
                  onClick={() => completePath(suggestion)}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-white/5 ${
                    idx === selectedPathIndex ? 'bg-orange-500/10 text-orange-300' : 'text-gray-300'
                  }`}
                >
                  {suggestion.isDirectory ? (
                    <Folder size={14} className="text-orange-400 flex-shrink-0" />
                  ) : (
                    <File size={14} className="text-gray-500 flex-shrink-0" />
                  )}
                  <span className="truncate">{suggestion.path}</span>
                  <span className="text-[10px] text-gray-600 ml-auto flex-shrink-0">
                    {idx === selectedPathIndex ? 'Tab ↹' : ''}
                  </span>
                </button>
              ))}
            </div>
          )}

          {/* # agent autocomplete dropdown */}
          {showAgentAutocomplete && agentSuggestions.length > 0 && (
            <div className="absolute bottom-full left-0 right-0 mb-1 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 max-h-48 overflow-y-auto">
              <div className="px-3 py-1.5 border-b border-ide-border">
                <span className="text-[10px] text-gray-600 uppercase tracking-wider">Agents</span>
              </div>
              {agentSuggestions.map((suggestion, idx) => (
                <button
                  key={suggestion.id}
                  type="button"
                  onClick={() => completeAgent(suggestion)}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-white/5 ${
                    idx === selectedAgentIndex ? 'bg-orange-500/10 text-orange-300' : 'text-gray-300'
                  }`}
                >
                  <Users size={14} className="text-purple-400 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="truncate block">{suggestion.name}</span>
                    {suggestion.description && (
                      <span className="text-[10px] text-gray-600 truncate block">{suggestion.description}</span>
                    )}
                  </div>
                  <span className="text-[10px] text-gray-600 ml-auto flex-shrink-0">
                    {idx === selectedAgentIndex ? 'Tab ↹' : ''}
                  </span>
                </button>
              ))}
            </div>
          )}

          {/* / command autocomplete dropdown */}
          {showCommandDropdown && commandSuggestions.length > 0 && (
            <div className="absolute bottom-full left-0 right-0 mb-1 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 max-h-48 overflow-y-auto">
              <div className="px-3 py-1.5 border-b border-ide-border">
                <span className="text-[10px] text-gray-600 uppercase tracking-wider">Commands</span>
              </div>
              {commandSuggestions.map((suggestion, idx) => (
                <button
                  key={suggestion.name}
                  type="button"
                  onClick={() => completeCommand(suggestion)}
                  className={`w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-white/5 ${
                    idx === selectedCommandIndex ? 'bg-orange-500/10 text-orange-300' : 'text-gray-300'
                  }`}
                >
                  <TerminalIcon size={14} className="text-green-400 flex-shrink-0" />
                  <div className="flex-1 min-w-0">
                    <span className="truncate block">/{suggestion.name}</span>
                    <span className="text-[10px] text-gray-600 truncate block">{suggestion.description}</span>
                  </div>
                  <span className="text-[10px] text-gray-600 ml-auto flex-shrink-0">
                    {idx === selectedCommandIndex ? 'Tab ↹' : ''}
                  </span>
                </button>
              ))}
            </div>
          )}

          {/* Unified input container */}
          <div className={`bg-[#111] border border-ide-border rounded-sl-2 overflow-hidden ${!activeSessionId ? 'opacity-50' : ''} ${isDragOver ? 'border-orange-500/50' : 'focus-within:border-orange-500/50'}`}>
            {/* Pending attachments preview strip */}
            {pendingAttachments.length > 0 && (
              <div className="flex flex-wrap gap-2 px-3 pt-2">
                {pendingAttachments.map((att) => (
                  <div
                    key={att.id}
                    className="relative group/att flex items-center gap-1.5 bg-white/5 border border-white/10 rounded-lg overflow-hidden"
                  >
                    {att.type === 'image' && att.previewUrl ? (
                      <img
                        src={att.previewUrl}
                        alt={att.name}
                        className="h-12 w-12 object-cover rounded-l-lg"
                      />
                    ) : (
                      <div className="h-12 w-10 flex items-center justify-center bg-white/5 rounded-l-lg">
                        <File size={16} className="text-gray-500" />
                      </div>
                    )}
                    <div className="px-2 py-1 max-w-[120px]">
                      <span className="text-[10px] text-gray-300 block truncate">{att.name}</span>
                      <span className="text-[9px] text-gray-600">
                        {att.size < 1024 ? `${att.size}B` : att.size < 1024 * 1024 ? `${(att.size / 1024).toFixed(1)}KB` : `${(att.size / (1024 * 1024)).toFixed(1)}MB`}
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={() => removeAttachment(att.id)}
                      className="absolute -top-1 -right-1 w-4 h-4 bg-red-500 rounded-full flex items-center justify-center opacity-0 group-hover/att:opacity-100 transition-opacity"
                      title="Remove"
                    >
                      <X size={10} className="text-white" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            {/* Drag-and-drop overlay */}
            {isDragOver && (
              <div className="absolute inset-0 bg-orange-500/10 border-2 border-dashed border-orange-500/50 rounded-sl-2 z-50 flex items-center justify-center pointer-events-none">
                <div className="flex items-center gap-2 text-orange-400">
                  <Paperclip size={20} />
                  <span className="text-sm font-medium">Drop files here</span>
                </div>
              </div>
            )}

            {/* Hidden file input */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept="image/png,image/jpeg,image/gif,image/webp,.txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.yaml,.yml,.toml,.css,.html,.xml,.csv,.log,.sh,.bash,.rs,.go,.java,.c,.cpp,.h,.hpp,.rb,.swift,.kt,.sql"
              onChange={handleFileInputChange}
              className="hidden"
            />

            {/* Textarea — borderless inside container */}
            <textarea
              ref={textareaRef}
              value={inputValue}
              onChange={handleInputChange}
              onKeyDown={handleKeyDown}
              onPaste={handlePaste}
              onDragOver={handleDragOver}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              disabled={!activeSessionId}
              placeholder={activeSessionId ? '# for agents, @ for files, / for commands — paste images or drag files' : 'Select or create a session first...'}
              className={`w-full bg-transparent text-white px-4 pt-3 pb-1 focus:outline-none resize-none min-h-[44px] max-h-[120px] text-sm placeholder-gray-600 ${!activeSessionId ? 'cursor-not-allowed' : ''}`}
              rows={2}
            />

            {/* Bottom toolbar: Paperclip | Agent · Model | Send */}
            <div className="flex items-center px-2 pb-2 pt-0.5">
              {/* Paperclip button */}
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={!activeSessionId}
                className={`p-1.5 rounded-lg transition-colors ${
                  activeSessionId
                    ? 'text-gray-500 hover:text-orange-400 hover:bg-white/5'
                    : 'text-gray-700 cursor-not-allowed'
                }`}
                title="Attach files or images"
              >
                <Paperclip size={16} />
              </button>

              {/* Agent & Model selectors — centered */}
              <div className="flex-1 flex items-center justify-center gap-3 text-[10px]">
                {/* Agent selector */}
                <div className="relative" ref={footerAgentRef}>
                  <button
                    type="button"
                    onClick={() => { setShowFooterAgentDropdown(!showFooterAgentDropdown); setShowFooterModelDropdown(false); }}
                    className="text-gray-500 hover:text-gray-300 transition-colors flex items-center gap-1"
                    disabled={!activeSessionId}
                  >
                    <Users size={10} />
                    <span>Agent: <span className="text-gray-400">{displayAgent}</span></span>
                    {activeStoreSession?.modes && activeStoreSession.modes.length > 0 && (
                      <ChevronUp size={8} className="text-gray-600" />
                    )}
                  </button>
                  {showFooterAgentDropdown && activeStoreSession?.modes && activeStoreSession.modes.length > 0 && (
                    <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 w-56 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 max-h-48 overflow-y-auto">
                      <div className="px-3 py-1.5 border-b border-ide-border">
                        <span className="text-[10px] text-gray-600 uppercase tracking-wider">Switch Agent</span>
                      </div>
                      {activeStoreSession.modes.map((mode) => (
                        <button
                          key={mode.id}
                          type="button"
                          onClick={() => {
                            onAgentChange(mode.id);
                            setShowFooterAgentDropdown(false);
                          }}
                          className={`w-full px-3 py-1.5 text-left text-xs hover:bg-white/5 transition-colors flex items-center gap-2 ${
                            activeStoreSession.currentModeId === mode.id ? 'bg-orange-500/10 text-orange-300' : 'text-gray-300'
                          }`}
                        >
                          <Users size={10} className="text-purple-400 flex-shrink-0" />
                          <span className="truncate">{mode.name}</span>
                          {activeStoreSession.currentModeId === mode.id && <Check size={10} className="ml-auto text-orange-400" />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>

                <span className="text-gray-700">·</span>

                {/* Model selector */}
                <div className="relative" ref={footerModelRef}>
                  <button
                    type="button"
                    onClick={() => { setShowFooterModelDropdown(!showFooterModelDropdown); setShowFooterAgentDropdown(false); }}
                    className="text-gray-500 hover:text-gray-300 transition-colors flex items-center gap-1"
                    disabled={!activeSessionId}
                  >
                    <Cpu size={10} />
                    <span>Model: <span className="text-gray-400">{displayModel}</span></span>
                    {activeStoreSession?.models && activeStoreSession.models.length > 0 && (
                      <ChevronUp size={8} className="text-gray-600" />
                    )}
                  </button>
                  {showFooterModelDropdown && activeStoreSession?.models && activeStoreSession.models.length > 0 && (
                    <div className="absolute bottom-full left-1/2 -translate-x-1/2 mb-1 w-56 bg-[#111] border border-ide-border rounded-lg shadow-2xl z-50 max-h-48 overflow-y-auto">
                      <div className="px-3 py-1.5 border-b border-ide-border">
                        <span className="text-[10px] text-gray-600 uppercase tracking-wider">Switch Model</span>
                      </div>
                      {activeStoreSession.models.map((model) => (
                        <button
                          key={model.id}
                          type="button"
                          onClick={() => {
                            onModelChange?.(model.id);
                            setShowFooterModelDropdown(false);
                          }}
                          className={`w-full px-3 py-1.5 text-left text-xs hover:bg-white/5 transition-colors flex items-center gap-2 ${
                            activeStoreSession.model === model.id ? 'bg-orange-500/10 text-orange-300' : 'text-gray-300'
                          }`}
                        >
                          <Cpu size={10} className="text-blue-400 flex-shrink-0" />
                          <span className="truncate">{model.name}</span>
                          {activeStoreSession.model === model.id && <Check size={10} className="ml-auto text-orange-400" />}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              </div>

              {/* Send button */}
              <button
                type="submit"
                disabled={(!inputValue.trim() && pendingAttachments.length === 0) || isGenerating || !activeSessionId}
                className={`p-2 rounded-lg transition-colors ${
                  (inputValue.trim() || pendingAttachments.length > 0) && !isGenerating
                    ? 'bg-orange-500 text-white hover:bg-orange-400'
                    : 'bg-transparent text-gray-700 cursor-not-allowed'
                }`}
              >
                <Send size={18} />
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
};

export default ChatPanel;
