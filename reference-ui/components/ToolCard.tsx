/**
 * ToolCard - Expandable card for displaying tool calls in chat.
 *
 * Shows tool name, purpose, status indicator.
 * Expands to show parameters JSON and result when completed.
 * Embeds ApprovalDialog when pending approval.
 */

import { useState } from 'react';
import { Wrench, Clock, CheckCircle2, XCircle, ChevronDown, ChevronRight } from 'lucide-react';
import type { ToolCall } from '../stores/sessionStore';
import { ApprovalDialog } from './ApprovalDialog';

// Status icon component based on tool call status
function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'pending':
    case 'running':
      return <Clock size={14} className="text-yellow-500 animate-pulse" />;
    case 'completed':
      return <CheckCircle2 size={14} className="text-green-500" />;
    case 'failed':
      return <XCircle size={14} className="text-red-500" />;
    default:
      return <Clock size={14} className="text-yellow-500 animate-pulse" />;
  }
}

interface ToolCardProps {
  toolCall: ToolCall;
  sessionId: string;
  isPending?: boolean;
}

export function ToolCard({ toolCall, sessionId, isPending = false }: ToolCardProps) {
  const [expanded, setExpanded] = useState(false);

  // Extract content-related params for potential future DiffView
  const p = toolCall.parameters;
  const { content, file_text, newStr, new_str, oldStr, old_str, ...restParams } = p as Record<string, unknown>;
  const hasParams = Object.keys(restParams).length > 0;

  return (
    <div className="rounded-lg border border-ide-border bg-black/40 text-sm">
      {/* Header - clickable to expand */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 px-3 py-2 hover:bg-white/5 text-left rounded-lg transition-colors"
      >
        <Wrench size={14} className="text-gray-400 flex-shrink-0" />
        <span className="font-medium text-gray-300">{toolCall.toolName}</span>
        {toolCall.purpose && (
          <span className="text-xs text-gray-600 truncate flex-1">— {toolCall.purpose}</span>
        )}
        <span className="ml-auto"><StatusIcon status={toolCall.status} /></span>
        {expanded ? (
          <ChevronDown size={14} className="text-gray-600 flex-shrink-0" />
        ) : (
          <ChevronRight size={14} className="text-gray-600 flex-shrink-0" />
        )}
      </button>

      {/* Expanded content */}
      {expanded && (
        <div className="border-t border-ide-border px-3 py-2 space-y-2">
          {/* Parameters */}
          {hasParams && (
            <div>
              <p className="text-xs text-gray-600 mb-1">Parameters:</p>
              <pre className="overflow-x-auto text-xs text-gray-500 bg-black p-2 rounded max-h-48">
                {JSON.stringify(restParams, null, 2)}
              </pre>
            </div>
          )}

          {/* Content preview (for file operations) */}
          {(content || file_text || newStr || new_str) && (
            <div>
              <p className="text-xs text-gray-600 mb-1">Content:</p>
              <pre className="overflow-x-auto text-xs text-gray-500 bg-black p-2 rounded max-h-48 whitespace-pre-wrap">
                {String(content || file_text || newStr || new_str).slice(0, 500)}
                {String(content || file_text || newStr || new_str).length > 500 && '...'}
              </pre>
            </div>
          )}

          {/* Result */}
          {toolCall.result && (
            <div>
              <p className="text-xs text-gray-600 mb-1">Result:</p>
              <pre className="overflow-x-auto text-xs text-gray-400 bg-black p-2 rounded max-h-48 whitespace-pre-wrap">
                {toolCall.result.slice(0, 1000)}
                {toolCall.result.length > 1000 && '...'}
              </pre>
            </div>
          )}
        </div>
      )}

      {/* Approval dialog when pending */}
      {isPending && toolCall.requiresApproval && (
        <ApprovalDialog toolCall={toolCall} sessionId={sessionId} />
      )}
    </div>
  );
}
