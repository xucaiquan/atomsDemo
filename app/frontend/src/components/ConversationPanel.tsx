/**
 * ConversationPanel —— 对话记录（US3 / T040）。
 * 展示用户要求与系统响应；助手消息只存摘要，不重复存 HTML。
 */
import { Bot, User } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { ConversationMessage } from '@/lib/atoms';

interface ConversationPanelProps {
  messages: ConversationMessage[];
}

export default function ConversationPanel({ messages }: ConversationPanelProps) {
  if (!messages.length) return null;

  return (
    <div className="space-y-2.5">
      {messages.map((message, index) => (
        <div
          key={`${message.role}-${index}`}
          className={cn('flex gap-2', message.role === 'user' && 'flex-row-reverse')}
        >
          <div
            className={cn(
              'flex h-6 w-6 shrink-0 items-center justify-center rounded-full',
              message.role === 'user'
                ? 'bg-sky-500/20 text-sky-300'
                : 'bg-indigo-500/20 text-indigo-300',
            )}
          >
            {message.role === 'user' ? (
              <User className="h-3.5 w-3.5" />
            ) : (
              <Bot className="h-3.5 w-3.5" />
            )}
          </div>
          <div
            className={cn(
              'max-w-[85%] rounded-xl px-3 py-2 text-xs leading-relaxed',
              message.role === 'user'
                ? 'bg-sky-500/10 text-slate-200'
                : 'bg-slate-800/70 text-slate-300',
            )}
          >
            <p className="whitespace-pre-wrap break-words">{message.content}</p>
            {message.version_seq != null && (
              <span className="mt-1 inline-block rounded bg-slate-700/60 px-1.5 py-0.5 text-[10px] text-slate-400">
                版本 v{message.version_seq}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
