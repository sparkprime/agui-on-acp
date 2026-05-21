import React from 'react';
import {
  Settings,
  Sun,
  Sparkles,
  Terminal,
  Plus,
  Folder,
  UserCircle,
  Bot
} from 'lucide-react';
import { ToolId, ToolDefinition } from '../types';

interface SidebarProps {
  activeTool: ToolId;
  onToolSelect: (toolId: ToolId) => void;
  onRequestAgentSwitch?: (targetTool: ToolId) => void;
  onChangeProject?: () => void;
  projectDir?: string | null;
  onOpenProfile?: () => void;
}

const Sidebar: React.FC<SidebarProps> = ({ activeTool, onToolSelect, onRequestAgentSwitch, onChangeProject, projectDir, onOpenProfile }) => {
  // Handler for tool clicks - opens dialog for different agents, does nothing for current
  const handleToolClick = (toolId: ToolId) => {
    if (toolId === activeTool) {
      return;
    }
    if (onRequestAgentSwitch) {
      onRequestAgentSwitch(toolId);
    } else {
      onToolSelect(toolId);
    }
  };

  // Definition of CLI tools for easy expansion
  const cliTools: ToolDefinition[] = [
    { id: 'claude', name: 'Claude Code', icon: <Sun size={24} />, color: 'text-orange-500' },
    { id: 'gemini', name: 'Gemini CLI', icon: <Sparkles size={24} />, color: 'text-blue-400' },
    { id: 'codex', name: 'Codex CLI', icon: <Terminal size={24} />, color: 'text-purple-400' },
    {
      id: 'agent',
      name: 'Your Agent',
      icon: (
        <div className="w-6 h-6 rounded-[6px] bg-[#7c3aed] flex items-center justify-center shadow-sm">
           <Bot size={15} className="text-white" />
        </div>
      ),
      color: 'text-white'
    },
  ];

  // Get project name from path
  const projectName = projectDir ? projectDir.split('/').pop() || 'Project' : '';

  return (
    <div className="w-16 h-full bg-ide-sidebar border-r border-ide-border flex flex-col items-center py-4 justify-between z-20 shadow-xl">
      <div className="flex flex-col gap-4 w-full items-center">

        {/* Project Folder Button */}
        {onChangeProject && projectDir && (
          <div className="flex flex-col gap-1 w-full items-center pb-4 border-b border-ide-border/50">
            <div
              onClick={onChangeProject}
              className="relative p-2.5 rounded-xl cursor-pointer hover:bg-white/5 text-ide-accent hover:text-ide-textLight transition-all duration-200 group"
              title={`Change Project\nCurrent: ${projectName}`}
            >
              <Folder size={24} />
              {/* Small indicator dot */}
              <div className="absolute bottom-1 right-1 w-2 h-2 bg-green-500 rounded-full" />
            </div>
          </div>
        )}

        {/* Agent/CLI Switcher Section */}
        <div className="flex flex-col gap-3 w-full items-center pb-4 border-b border-ide-border/50">
           {cliTools.map((tool) => (
             <div
               key={tool.id}
               onClick={() => handleToolClick(tool.id)}
               className={`relative p-2.5 rounded-xl cursor-pointer transition-all duration-200 group ${
                 activeTool === tool.id
                   ? 'bg-white/10 shadow-lg'
                   : 'hover:bg-white/5'
               }`}
               title={activeTool === tool.id ? tool.name : `Switch to ${tool.name}`}
             >
               {/* Icon with specific brand color */}
               <div className={`${tool.color} transition-transform group-hover:scale-110 flex items-center justify-center`}>
                 {tool.icon}
               </div>

               {/* Active Indicator Bar (Left) */}
               {activeTool === tool.id && (
                 <div className="absolute left-0 top-1/2 -translate-y-1/2 w-1 h-6 bg-ide-textLight rounded-r-full -ml-2" />
               )}
             </div>
           ))}

           {/* Add New Tool Button */}
           <div className="p-2.5 rounded-xl cursor-pointer hover:bg-white/5 text-gray-500 hover:text-gray-300 transition-colors" title="Add CLI Tool">
              <Plus size={20} />
           </div>
        </div>
      </div>

      <div className="flex flex-col gap-4">
        <div onClick={onOpenProfile} title="Profile">
          <SidebarIcon icon={<UserCircle size={22} />} />
        </div>
        <SidebarIcon icon={<Settings size={22} />} />
      </div>
    </div>
  );
};

interface SidebarIconProps {
  icon: React.ReactNode;
  active?: boolean;
}

const SidebarIcon: React.FC<SidebarIconProps> = ({ icon, active }) => {
  return (
    <div className={`p-2.5 rounded-lg cursor-pointer transition-colors duration-200 ${
      active ? 'text-ide-textLight bg-white/10' : 'text-gray-500 hover:text-ide-text hover:bg-white/5'
    }`}>
      {icon}
    </div>
  );
};

export default Sidebar;
