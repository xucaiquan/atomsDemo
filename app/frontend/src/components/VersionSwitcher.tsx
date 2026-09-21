/**
 * VersionSwitcher —— 版本列表与切换（US3 / FR-007）。
 */
import { GitCommitHorizontal } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { VersionBrief } from '@/lib/atoms';

interface VersionSwitcherProps {
  versions: VersionBrief[];
  activeSeq: number | null;
  onSelect: (seq: number) => void;
}

export default function VersionSwitcher({ versions, activeSeq, onSelect }: VersionSwitcherProps) {
  if (versions.length <= 1) return null;

  return (
    <div className="flex items-center gap-1.5 overflow-x-auto">
      <GitCommitHorizontal className="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {versions.map((version) => (
        <button
          key={version.seq}
          type="button"
          onClick={() => onSelect(version.seq)}
          className={cn(
            'shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] transition-colors',
            activeSeq === version.seq
              ? 'border-sky-500/60 bg-sky-500/15 text-sky-300'
              : 'border-slate-700 bg-slate-800/50 text-slate-400 hover:border-slate-600 hover:text-slate-300',
          )}
        >
          v{version.seq}
          {version.status === 'failed' && <span className="ml-1 text-rose-400">✕</span>}
          {version.status === 'cancelled' && <span className="ml-1 text-amber-400">⊘</span>}
        </button>
      ))}
    </div>
  );
}
