/**
 * ApprovalDialog - Permission request UI for tool calls requiring approval.
 *
 * Shows permission options (allow_once, allow_always, reject_once, reject_always)
 * with styled buttons based on option kind.
 */

import { AlertTriangle } from 'lucide-react';
import type { ToolCall, PermissionOption } from '../stores/sessionStore';

// Button styles based on permission option kind
const KIND_STYLE: Record<string, string> = {
  allow_once: 'bg-green-600 hover:bg-green-500',
  allow_always: 'bg-orange-600 hover:bg-orange-500',
  reject_once: 'bg-red-600 hover:bg-red-500',
  reject_always: 'bg-red-800 hover:bg-red-700',
};

interface ApprovalDialogProps {
  toolCall: ToolCall;
  sessionId: string;
  onResponse?: (optionId: string) => void;
}

export function ApprovalDialog({ toolCall, sessionId, onResponse }: ApprovalDialogProps) {
  const options = toolCall.permissionOptions ?? [];

  function respond(optionId: string) {
    onResponse?.(optionId);
  }

  return (
    <div className="border-t border-orange-500/20 bg-orange-500/5 px-3 py-2">
      <p className="mb-2 text-xs text-orange-400 flex items-center gap-1.5">
        <AlertTriangle size={14} className="text-orange-400 flex-shrink-0" />
        This tool requires approval
      </p>
      <div className="flex flex-wrap gap-2">
        {options.length > 0 ? (
          options.map((opt) => (
            <button
              key={opt.id}
              onClick={() => respond(opt.id)}
              className={`rounded px-3 py-1 text-xs text-white ${KIND_STYLE[opt.kind ?? ''] ?? 'bg-gray-700 hover:bg-gray-600'}`}
              title={opt.description}
            >
              {opt.label}
            </button>
          ))
        ) : (
          <>
            <button
              onClick={() => respond('allow_once')}
              className="rounded bg-green-700 px-3 py-1 text-xs text-white hover:bg-green-600"
            >
              Approve
            </button>
            <button
              onClick={() => respond('reject_once')}
              className="rounded bg-red-700 px-3 py-1 text-xs text-white hover:bg-red-600"
            >
              Reject
            </button>
          </>
        )}
      </div>
    </div>
  );
}
