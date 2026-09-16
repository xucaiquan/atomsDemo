/**
 * PromptInput —— 描述输入与提交（US1 / T022）。
 *
 * - 生成中禁用重复提交（配合后端 409 并发约束）
 * - 失败时不清空输入（FR-011 / SC-007：用户已输入内容保留率 100%）
 */
import { Loader2, Send, Sparkles } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { PROMPT_MAX_LEN_HINT } from '@/lib/constants';

interface PromptInputProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  isGenerating: boolean;
  hasProject: boolean;
}

const EXAMPLES = [
  '做一个记账小工具，能记收入和支出，显示总余额',
  '做一个番茄钟，可以设置专注时长并统计今日完成次数',
  '做一个待办清单，支持添加、勾选完成和按状态筛选',
];

export default function PromptInput({
  value,
  onChange,
  onSubmit,
  isGenerating,
  hasProject,
}: PromptInputProps) {
  const trimmed = value.trim();
  const canSubmit = trimmed.length > 0 && !isGenerating;

  return (
    <div className="space-y-2.5">
      <div className="relative">
        <Textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={
            hasProject
              ? '在已有应用上继续提要求，例如「再加一个按月份筛选的图表」'
              : '描述你想要的应用，例如「做一个记账小工具，能记收入和支出，显示总余额」'
          }
          className="min-h-[92px] resize-none border-slate-700/70 bg-slate-900/60 text-sm text-slate-100 placeholder:text-slate-500 focus-visible:border-sky-500/60"
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === 'Enter' && canSubmit) {
              e.preventDefault();
              onSubmit();
            }
          }}
        />
        <span className="absolute bottom-2 right-3 text-[10px] text-slate-600">
          {trimmed.length} / {PROMPT_MAX_LEN_HINT} · Ctrl+Enter 提交
        </span>
      </div>

      {!hasProject && !trimmed && (
        <div className="flex flex-wrap gap-1.5">
          {EXAMPLES.map((example) => (
            <button
              key={example}
              type="button"
              onClick={() => onChange(example)}
              className="rounded-full border border-slate-700/70 bg-slate-800/50 px-2.5 py-1 text-[11px] text-slate-400 transition-colors hover:border-sky-500/50 hover:text-sky-300"
            >
              {example.slice(0, 16)}…
            </button>
          ))}
        </div>
      )}

      <Button
        onClick={onSubmit}
        disabled={!canSubmit}
        className="w-full gap-2 bg-gradient-to-r from-sky-500 to-indigo-500 text-white hover:from-sky-400 hover:to-indigo-400 disabled:opacity-40"
      >
        {isGenerating ? (
          <>
            <Loader2 className="h-4 w-4 animate-spin" />
            智能体工作中…
          </>
        ) : (
          <>
            {hasProject ? <Sparkles className="h-4 w-4" /> : <Send className="h-4 w-4" />}
            {hasProject ? '生成新版本' : '生成应用'}
          </>
        )}
      </Button>
    </div>
  );
}
