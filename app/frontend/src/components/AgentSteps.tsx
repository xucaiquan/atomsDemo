/**
 * AgentSteps —— 智能体工作流可视化（US4 / FR-008）。
 *
 * 展示型组件：由 props 接收步骤数组并渲染，状态流转「等待 → 进行中 → 已完成/失败」。
 * 失败时在**对应步骤**上显示可读原因，而非只在全局提示。
 */
import { AlertCircle, Ban, Check, Circle, Loader2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { GenerationStep } from '@/lib/atoms';

interface AgentStepsProps {
  steps: GenerationStep[];
  failedMessage?: string | null;
  compact?: boolean;
}

const STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  running: '进行中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

/** 从步骤原始产出里截取一小段作为「思考片段」展示。 */
function thinkingSnippet(step: GenerationStep): string {
  const raw = (step.output || '').replace(/\s+/g, ' ').trim();
  if (!raw) return '';
  if (step.seq === 3) return '已产出完整页面源码';
  return raw.length > 120 ? `${raw.slice(0, 120)}…` : raw;
}

function StepIcon({ status }: { status: GenerationStep['status'] }) {
  if (status === 'running') return <Loader2 className="h-4 w-4 animate-spin text-sky-400" />;
  if (status === 'succeeded') return <Check className="h-4 w-4 text-emerald-400" />;
  if (status === 'failed') return <AlertCircle className="h-4 w-4 text-rose-400" />;
  if (status === 'cancelled') return <Ban className="h-4 w-4 text-amber-400" />;
  return <Circle className="h-4 w-4 text-slate-600" />;
}

export default function AgentSteps({ steps, failedMessage, compact }: AgentStepsProps) {
  if (!steps.length) return null;

  return (
    <div className="space-y-2">
      {steps.map((step, index) => {
        const snippet = thinkingSnippet(step);
        return (
          <div
            key={step.seq}
            className={cn(
              'relative rounded-xl border px-3 py-2.5 transition-colors',
              step.status === 'running' && 'border-sky-500/40 bg-sky-500/5',
              step.status === 'succeeded' && 'border-emerald-500/25 bg-emerald-500/5',
              step.status === 'failed' && 'border-rose-500/40 bg-rose-500/5',
              step.status === 'cancelled' && 'border-amber-500/30 bg-amber-500/5',
              step.status === 'pending' && 'border-slate-800 bg-slate-900/40',
            )}
          >
            <div className="flex items-center gap-2.5">
              <StepIcon status={step.status} />
              <span
                className={cn(
                  'text-sm font-medium',
                  step.status === 'pending' ? 'text-slate-500' : 'text-slate-100',
                )}
              >
                {step.seq}. {step.name}
              </span>
              <span
                className={cn(
                  'ml-auto rounded-full px-2 py-0.5 text-[10px]',
                  step.status === 'running' && 'bg-sky-500/15 text-sky-300',
                  step.status === 'succeeded' && 'bg-emerald-500/15 text-emerald-300',
                  step.status === 'failed' && 'bg-rose-500/15 text-rose-300',
                  step.status === 'cancelled' && 'bg-amber-500/15 text-amber-300',
                  step.status === 'pending' && 'bg-slate-800 text-slate-500',
                )}
              >
                {STATUS_LABEL[step.status]}
              </span>
            </div>

            {step.status === 'failed' && (
              <p className="mt-2 pl-6 text-xs leading-relaxed text-rose-300">
                {step.output || failedMessage || '该阶段执行失败'}
              </p>
            )}

            {!compact && step.status !== 'failed' && snippet && (
              <p className="mt-2 pl-6 text-xs leading-relaxed text-slate-400">{snippet}</p>
            )}

            {!compact && step.status === 'running' && !snippet && (
              <p className="mt-2 pl-6 text-xs text-slate-500">智能体正在思考…</p>
            )}

            {index < steps.length - 1 && (
              <span className="absolute -bottom-2 left-[22px] h-2 w-px bg-slate-800" />
            )}
          </div>
        );
      })}
    </div>
  );
}
