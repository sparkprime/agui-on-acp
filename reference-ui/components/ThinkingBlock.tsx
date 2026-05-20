/**
 * ThinkingBlock — Collapsible "thinking" block similar to ChatGPT's
 * "Thought for X seconds" UI.
 *
 * Shows a compact header when collapsed, and reveals the full thinking
 * text (rendered as markdown) when expanded.
 */

import { useState } from 'react';
import { ChevronRight, Brain } from 'lucide-react';
import ReactMarkdown from 'react-markdown';

interface ThinkingBlockProps {
  content: string;
  durationMs?: number;
  isStreaming: boolean;
}

/**
 * Format a duration in milliseconds to a human-readable string.
 */
function formatThinkingDuration(ms: number): string {
  const seconds = Math.round(ms / 1000);
  if (seconds < 2) return 'a few seconds';
  if (seconds < 60) return `${seconds} seconds`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  if (minutes === 1) {
    return remainingSeconds > 0 ? `1 minute ${remainingSeconds}s` : '1 minute';
  }
  return remainingSeconds > 0
    ? `${minutes} minutes ${remainingSeconds}s`
    : `${minutes} minutes`;
}

export function ThinkingBlock({ content, durationMs, isStreaming }: ThinkingBlockProps) {
  const [expanded, setExpanded] = useState(false);

  const headerText = isStreaming
    ? 'Thinking'
    : durationMs != null
      ? `Thought for ${formatThinkingDuration(durationMs)}`
      : 'Thought';

  return (
    <div className="max-w-[90%]">
      {/* Collapsible header */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 py-1.5 px-1 text-sm text-gray-500 hover:text-gray-300 transition-colors group"
      >
        <ChevronRight
          size={14}
          className={`transition-transform duration-200 ${expanded ? 'rotate-90' : ''}`}
        />
        <Brain size={14} className="text-gray-600 group-hover:text-gray-400" />
        <span>
          {headerText}
          {isStreaming && (
            <span className="inline-flex ml-1">
              <span className="animate-pulse">...</span>
            </span>
          )}
        </span>
      </button>

      {/* Expanded content */}
      {expanded && content && (
        <div className="ml-8 mt-1 mb-2 pl-3 border-l-2 border-gray-800">
          <div className="prose prose-invert prose-sm max-w-none text-gray-500 prose-p:my-1.5 prose-headings:text-gray-400 prose-headings:mt-3 prose-headings:mb-1 prose-code:text-gray-400 prose-pre:bg-black/40 prose-pre:border prose-pre:border-white/5">
            <ReactMarkdown>{content}</ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  );
}
