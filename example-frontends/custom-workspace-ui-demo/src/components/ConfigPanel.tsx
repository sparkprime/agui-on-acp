import React, { useState } from 'react';
import { X, Users, Sparkles, Settings, RotateCcw, Cpu, Wrench, Bot } from 'lucide-react';
import { ToolId } from '../types';

const DEFAULT_AGENT_FALLBACK = 'default';

interface ConfigPanelProps {
  isOpen: boolean;
  onClose: () => void;
  activeTool: ToolId;
  defaultAgentId: string;
  onDefaultAgentChange: (agentId: string) => void;
  availableAgents: Array<{ id: string; name: string; description?: string }>;
}

const ConfigPanel: React.FC<ConfigPanelProps> = ({
  isOpen,
  onClose,
  activeTool,
  defaultAgentId,
  onDefaultAgentChange,
  availableAgents,
}) => {
  const [customAgentInput, setCustomAgentInput] = useState('');
  const [showCustomInput, setShowCustomInput] = useState(false);

  const getToolName = (id: ToolId) => {
    switch(id) {
      case 'claude': return 'Claude Code';
      case 'gemini': return 'Gemini CLI';
      case 'codex': return 'Codex CLI';
      case 'agent': return 'Agent';
      default: return 'CLI Tool';
    }
  };

  const handleAgentSelect = (agentId: string) => {
    if (agentId === '__custom__') {
      setShowCustomInput(true);
      return;
    }
    setShowCustomInput(false);
    onDefaultAgentChange(agentId);
  };

  const handleCustomAgentSubmit = () => {
    const trimmed = customAgentInput.trim();
    if (trimmed) {
      onDefaultAgentChange(trimmed);
      setCustomAgentInput('');
      setShowCustomInput(false);
    }
  };

  const handleReset = () => {
    onDefaultAgentChange(DEFAULT_AGENT_FALLBACK);
    setShowCustomInput(false);
    setCustomAgentInput('');
  };

  // Check if current default is in the available agents list
  const isCurrentInList = availableAgents.some((a) => a.id === defaultAgentId);
  const currentAgentName = availableAgents.find((a) => a.id === defaultAgentId)?.name;

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm animate-in fade-in duration-200">
      <div className="w-[560px] bg-black rounded-xl border border-ide-border shadow-2xl flex flex-col overflow-hidden">

        {/* Header */}
        <div className="h-14 border-b border-ide-border flex items-center justify-between px-6">
          <div className="flex items-center gap-3">
            <Users size={18} className="text-orange-500" />
            <h3 className="font-medium text-lg text-white">{getToolName(activeTool)} — Agents</h3>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 hover:bg-white/5 rounded-full text-gray-500 hover:text-white transition-colors"
          >
            <X size={20} />
          </button>
        </div>

        {/* Content */}
        <div className="p-6 space-y-4">
          <p className="text-xs text-gray-600">
            Available agents for this workspace.
          </p>

          {/* Agent Card */}
          <div className="flex items-center justify-between p-4 bg-[#0A0A0A] border border-ide-border rounded-xl">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-lg flex items-center justify-center bg-orange-500/10">
                <Bot size={20} className="text-orange-500" />
              </div>
              <div>
                <span className="font-medium text-white block">ACP Agent</span>
                <span className="text-xs text-gray-600">AI coding agent</span>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <span className="text-xs text-gray-500 bg-white/5 px-2 py-1 rounded font-mono">active</span>
              <div className="w-2 h-2 rounded-full bg-green-500" />
            </div>
          </div>

          {/* Default Agent Setting */}
          <div className="p-4 bg-[#0A0A0A] border border-ide-border rounded-xl space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <Settings size={14} className="text-gray-400" />
                <span className="text-sm font-medium text-white">Default Agent for New Sessions</span>
              </div>
              <button
                onClick={handleReset}
                className="flex items-center gap-1.5 text-xs text-gray-500 hover:text-orange-400 transition-colors"
                title={`Reset to ${DEFAULT_AGENT_FALLBACK}`}
              >
                <RotateCcw size={12} />
                <span>Reset</span>
              </button>
            </div>

            <p className="text-xs text-gray-600">
              This agent will be automatically selected when creating every new session.
            </p>

            {/* Current default display */}
            <div className="bg-black/50 border border-white/[0.06] rounded-lg overflow-hidden">
              <div className="flex items-center gap-2 px-3 py-2">
                <div className="w-2 h-2 rounded-full bg-orange-500 flex-shrink-0" />
                <span className="text-sm text-white font-mono truncate">
                  {defaultAgentId}
                </span>
                {currentAgentName && currentAgentName !== defaultAgentId && (
                  <span className="text-xs text-gray-500 ml-1">({currentAgentName})</span>
                )}
              </div>
            </div>

            {/* Agent selector */}
            {availableAgents.length > 0 && (
              <div className="space-y-2">
                <label className="text-xs text-gray-500">
                  Select from available agents:
                </label>
                <select
                  value={showCustomInput ? '__custom__' : (isCurrentInList ? defaultAgentId : '__custom__')}
                  onChange={(e) => handleAgentSelect(e.target.value)}
                  className="w-full px-3 py-2 bg-black border border-white/[0.08] rounded-lg text-sm text-white
                             focus:outline-none focus:border-orange-500/50 transition-colors appearance-none cursor-pointer"
                >
                  {availableAgents.map((agent) => (
                    <option key={agent.id} value={agent.id}>
                      {agent.name || agent.id}
                    </option>
                  ))}
                  <option value="__custom__">Custom agent ID...</option>
                </select>
              </div>
            )}

            {/* Custom agent ID input */}
            {(showCustomInput || availableAgents.length === 0) && (
              <div className="space-y-2">
                <label className="text-xs text-gray-500">
                  {availableAgents.length === 0 ? 'Enter agent ID:' : 'Enter custom agent ID:'}
                </label>
                <div className="flex gap-2">
                  <input
                    type="text"
                    value={customAgentInput}
                    onChange={(e) => setCustomAgentInput(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleCustomAgentSubmit()}
                    placeholder="e.g. default"
                    className="flex-1 px-3 py-2 bg-black border border-white/[0.08] rounded-lg text-sm text-white
                               placeholder:text-gray-700 focus:outline-none focus:border-orange-500/50 transition-colors font-mono"
                  />
                  <button
                    onClick={handleCustomAgentSubmit}
                    disabled={!customAgentInput.trim()}
                    className="px-4 py-2 bg-orange-500/10 border border-orange-500/20 rounded-lg text-sm text-orange-400
                               hover:bg-orange-500/20 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
                  >
                    Set
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Pointer to MCP */}
          <div className="flex items-center gap-2 p-3 bg-[#0A0A0A] border border-ide-border rounded-lg text-xs text-gray-500">
            <Sparkles size={14} className="text-purple-400 flex-shrink-0" />
            <span>
              MCP Servers and Commands are configured via the ACP backend.
            </span>
          </div>
        </div>
      </div>
    </div>
  );
};

export default ConfigPanel;
