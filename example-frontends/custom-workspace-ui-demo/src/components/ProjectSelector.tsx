import React, { useState, useEffect } from 'react';
import { Bot } from 'lucide-react';

// Motivational texts for animated carousel
const motivationalTexts = [
  "How can I help you code today?",
  "Let's build something amazing",
  "What will you create today?",
  "Turn your ideas into reality",
  "Let's ship something great",
  "Ready to bring your vision to life?",
  "What's on your mind today?",
  "Let's write some beautiful code",
];

interface ProjectSelectorProps {
  onProjectSelect: (path: string) => void;
}

const RECENT_PROJECTS_KEY = 'acp-agui-recent-projects';
const MAX_RECENT_PROJECTS = 5;

// Get recent projects from localStorage
const getRecentProjects = (): string[] => {
  try {
    const stored = localStorage.getItem(RECENT_PROJECTS_KEY);
    return stored ? JSON.parse(stored) : [];
  } catch {
    return [];
  }
};

// Save recent project to localStorage
const saveRecentProject = (path: string): void => {
  try {
    const recent = getRecentProjects();
    // Remove if already exists, then add to front
    const filtered = recent.filter(p => p !== path);
    const updated = [path, ...filtered].slice(0, MAX_RECENT_PROJECTS);
    localStorage.setItem(RECENT_PROJECTS_KEY, JSON.stringify(updated));
  } catch (e) {
    console.error('Failed to save recent project:', e);
  }
};

const ProjectSelector: React.FC<ProjectSelectorProps> = ({ onProjectSelect }) => {
  const [projectPath, setProjectPath] = useState('');
  const [recentProjects, setRecentProjects] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  // Text carousel state
  const [textIndex, setTextIndex] = useState(0);
  const [isTextExiting, setIsTextExiting] = useState(false);

  useEffect(() => {
    setRecentProjects(getRecentProjects());
  }, []);

  // Text carousel rotation
  useEffect(() => {
    const interval = setInterval(() => {
      setIsTextExiting(true);
      setTimeout(() => {
        setTextIndex((prev) => (prev + 1) % motivationalTexts.length);
        setIsTextExiting(false);
      }, 250);
    }, 2500);
    return () => clearInterval(interval);
  }, []);

  const handleSubmit = () => {
    const trimmedPath = projectPath.trim();

    if (!trimmedPath) {
      setError('Please enter a project path');
      return;
    }

    // Basic validation - check if path looks reasonable
    if (!trimmedPath.startsWith('/') && !trimmedPath.match(/^[a-zA-Z]:\\/)) {
      setError('Please enter an absolute path (e.g., /Users/name/project or C:\\Users\\name\\project)');
      return;
    }

    setError(null);
    saveRecentProject(trimmedPath);
    onProjectSelect(trimmedPath);
  };

  const handleRecentSelect = (path: string) => {
    saveRecentProject(path);
    onProjectSelect(path);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleSubmit();
    }
  };

  return (
    <div className="flex items-center justify-center min-h-screen bg-ide-bg">
      <div className="max-w-md w-full mx-4">
        <div className="bg-ide-panel rounded-lg border border-ide-border p-8 shadow-lg">
          {/* Animated Icon */}
          <div className="flex flex-col items-center mb-6 space-y-3">
            <div
              className="w-20 h-20 rounded-full bg-ide-border flex items-center justify-center"
              style={{
                animation: 'icon-float 3s ease-in-out infinite, icon-glow 4s ease-in-out infinite',
              }}
            >
              <Bot size={40} className="text-orange-500" />
            </div>
            <h1 className="text-2xl font-bold text-ide-text text-center">
              ACP &rarr; AG-UI Bridge
            </h1>
            <p
              className={`text-base text-gray-600 text-center ${isTextExiting ? 'animate-text-fade-out' : 'animate-text-fade-in'}`}
              key={textIndex}
            >
              {motivationalTexts[textIndex]}
            </p>
          </div>

          {/* Description */}
          <div className="bg-ide-bg rounded-md p-4 mb-6">
            <p className="text-sm text-ide-textLight mb-2">
              Enter the path to your project directory:
            </p>
            <ul className="text-xs text-ide-textLight space-y-1 list-disc list-inside">
              <li>Use an empty directory for a new project</li>
              <li>Or enter an existing project path to continue</li>
              <li>All CLI-generated files will be stored here</li>
            </ul>
          </div>

          {/* Path Input */}
          <div className="mb-4">
            <input
              type="text"
              value={projectPath}
              onChange={(e) => {
                setProjectPath(e.target.value);
                setError(null);
              }}
              onKeyDown={handleKeyDown}
              placeholder="/path/to/your/project"
              className="w-full bg-ide-bg border border-ide-border rounded-md px-4 py-3 text-ide-text placeholder:text-ide-textLight/50 focus:outline-none focus:border-ide-accent transition-colors"
              autoFocus
            />
            {error && (
              <p className="text-red-400 text-xs mt-2">{error}</p>
            )}
          </div>

          {/* Open Button */}
          <button
            onClick={handleSubmit}
            className="w-full bg-ide-accent hover:bg-ide-accent/90 text-white font-medium py-3 px-4 rounded-md transition-colors flex items-center justify-center gap-2"
          >
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 19a2 2 0 01-2-2V7a2 2 0 012-2h4l2 2h4a2 2 0 012 2v1M5 19h14a2 2 0 002-2v-5a2 2 0 00-2-2H9a2 2 0 00-2 2v5a2 2 0 01-2 2z" />
            </svg>
            Open Project
          </button>

          {/* Recent Projects */}
          <div className="mt-6 pt-6 border-t border-ide-border">
            <p className="text-xs text-ide-textLight text-center mb-3">
              {recentProjects.length > 0 ? 'Recent Projects' : 'Recent projects will appear here'}
            </p>
            {recentProjects.length > 0 && (
              <div className="space-y-2">
                {recentProjects.map((path, index) => (
                  <button
                    key={index}
                    onClick={() => handleRecentSelect(path)}
                    className="w-full text-left px-3 py-2 text-sm text-ide-textLight hover:text-ide-text hover:bg-ide-bg rounded-md transition-colors truncate"
                    title={path}
                  >
                    <span className="flex items-center gap-2">
                      <svg className="w-4 h-4 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3 7v10a2 2 0 002 2h14a2 2 0 002-2V9a2 2 0 00-2-2h-6l-2-2H5a2 2 0 00-2 2z" />
                      </svg>
                      <span className="truncate">{path}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="mt-4 text-center">
          <p className="text-xs text-ide-textLight">
            Powered by ACP Agent
          </p>
        </div>
      </div>
    </div>
  );
};

export default ProjectSelector;
