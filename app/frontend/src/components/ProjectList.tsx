/**
 * ProjectList —— 历史项目列表（US2 / FR-005）。
 * 展示标题、更新时间、版本数与最新状态。
 */
import { FolderOpen, Loader2, Plus, Trash2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { formatRelative } from '@/lib/constants';
import type { ProjectBrief } from '@/lib/atoms';

interface ProjectListProps {
  projects: ProjectBrief[];
  activeId: string | null;
  loading: boolean;
  onSelect: (publicId: string) => void;
  onCreate: () => void;
  onDelete: (publicId: string) => void;
}

const STATUS_DOT: Record<string, string> = {
  succeeded: 'bg-emerald-400',
  running: 'bg-sky-400 animate-pulse',
  pending: 'bg-amber-400 animate-pulse',
  failed: 'bg-rose-400',
  cancelled: 'bg-amber-400',
};

export default function ProjectList({
  projects,
  activeId,
  loading,
  onSelect,
  onCreate,
  onDelete,
}: ProjectListProps) {
  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between px-3 pb-2">
        <h2 className="text-xs font-semibold uppercase tracking-wider text-slate-500">
          我的项目
        </h2>
        <Button
          size="sm"
          variant="ghost"
          onClick={onCreate}
          className="h-6 gap-1 px-2 text-[11px] text-sky-400 hover:bg-sky-500/10 hover:text-sky-300"
        >
          <Plus className="h-3.5 w-3.5" />
          新建
        </Button>
      </div>

      <div className="flex-1 space-y-1 overflow-y-auto px-2 pb-2">
        {loading && (
          <div className="flex items-center justify-center gap-2 py-8 text-xs text-slate-500">
            <Loader2 className="h-4 w-4 animate-spin" />
            加载项目列表…
          </div>
        )}

        {!loading && projects.length === 0 && (
          <p className="px-2 py-6 text-center text-xs text-slate-500">
            还没有项目，直接在右侧输入描述即可开始生成
          </p>
        )}

        {projects.map((project) => (
          <div
            key={project.public_id}
            role="button"
            tabIndex={0}
            onClick={() => onSelect(project.public_id)}
            onKeyDown={(e) => e.key === 'Enter' && onSelect(project.public_id)}
            className={cn(
              'group cursor-pointer rounded-lg border px-2.5 py-2 transition-colors',
              activeId === project.public_id
                ? 'border-sky-500/50 bg-sky-500/10'
                : 'border-transparent hover:border-slate-700 hover:bg-slate-800/50',
            )}
          >
            <div className="flex items-center gap-2">
              <FolderOpen
                className={cn(
                  'h-3.5 w-3.5 shrink-0',
                  activeId === project.public_id ? 'text-sky-400' : 'text-slate-500',
                )}
              />
              <span className="min-w-0 flex-1 truncate text-sm text-slate-200">
                {project.title}
              </span>
              <button
                type="button"
                aria-label="删除项目"
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(project.public_id);
                }}
                className="hidden shrink-0 rounded p-0.5 text-slate-500 hover:text-rose-400 group-hover:block"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
            <div className="mt-1 flex items-center gap-2 pl-5.5 text-[11px] text-slate-500">
              {project.latest_status && (
                <span className="flex items-center gap-1">
                  <span
                    className={cn(
                      'h-1.5 w-1.5 rounded-full',
                      STATUS_DOT[project.latest_status] || 'bg-slate-600',
                    )}
                  />
                  {project.latest_status === 'succeeded'
                    ? '已生成'
                    : project.latest_status === 'failed'
                      ? '上次失败'
                      : project.latest_status === 'cancelled'
                        ? '已取消'
                        : '生成中'}
                </span>
              )}
              <span>v{project.version_count}</span>
              <span>{formatRelative(project.updated_at)}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
