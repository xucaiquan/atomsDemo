/**
 * VersionSwitcher —— 版本列表、切换与回滚（US3 / FR-007 / S5.1）。
 *
 * 回滚入口采用「二次确认」交互：第一次点击变为「确认回滚 v{n}?」高亮态，
 * 再次点击才真正调用 restore（回滚即新版本，原历史全部保留）。
 */
import { useState } from 'react';
import { GitCommitHorizontal, Undo2 } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { VersionBrief } from '@/lib/atoms';

interface VersionSwitcherProps {
  versions: VersionBrief[];
  activeSeq: number | null;
  onSelect: (seq: number) => void;
  /** 回滚到指定版本（后端创建新版本，非破坏性）。 */
  onRestore: (seq: number) => void;
}

export default function VersionSwitcher({
  versions,
  activeSeq,
  onSelect,
  onRestore,
}: VersionSwitcherProps) {
  const [confirmSeq, setConfirmSeq] = useState<number | null>(null);

  if (versions.length <= 1) return null;

  const latestSeq = Math.max(...versions.map((v) => v.seq));

  return (
    <div className="flex items-center gap-1.5 overflow-x-auto">
      <GitCommitHorizontal className="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {versions.map((version) => (
        <span key={version.seq} className="flex shrink-0 items-center">
          <button
            type="button"
            onClick={() => {
              onSelect(version.seq);
              setConfirmSeq(null);
            }}
            className={cn(
              'rounded-l-full border px-2.5 py-0.5 text-[11px] transition-colors',
              activeSeq === version.seq
                ? 'border-sky-500/60 bg-sky-500/15 text-sky-300'
                : 'border-slate-700 bg-slate-800/50 text-slate-400 hover:border-slate-600 hover:text-slate-300',
            )}
          >
            v{version.seq}
            {version.status === 'failed' && <span className="ml-1 text-rose-400">✕</span>}
            {version.status === 'cancelled' && <span className="ml-1 text-amber-400">⊘</span>}
          </button>
          {/* 回滚入口：仅成功且非最新的版本可回滚 */}
          {version.status === 'succeeded' && version.seq !== latestSeq && (
            <button
              type="button"
              title={
                confirmSeq === version.seq
                  ? `再次点击确认：把 v${version.seq} 回滚为新版本`
                  : `回滚 v${version.seq}（生成新版本，历史保留）`
              }
              onClick={() => {
                if (confirmSeq === version.seq) {
                  onRestore(version.seq);
                  setConfirmSeq(null);
                } else {
                  setConfirmSeq(version.seq);
                  // 3 秒内未二次确认则自动退出确认态
                  window.setTimeout(
                    () => setConfirmSeq((cur) => (cur === version.seq ? null : cur)),
                    3000,
                  );
                }
              }}
              className={cn(
                'rounded-r-full border border-l-0 px-1.5 py-0.5 text-[10px] transition-colors',
                confirmSeq === version.seq
                  ? 'border-amber-500/60 bg-amber-500/15 text-amber-300'
                  : 'border-slate-700 bg-slate-800/50 text-slate-500 hover:border-slate-600 hover:text-slate-300',
              )}
            >
              {confirmSeq === version.seq ? (
                '确认回滚?'
              ) : (
                <Undo2 className="h-3 w-3" />
              )}
            </button>
          )}
        </span>
      ))}
    </div>
  );
}
