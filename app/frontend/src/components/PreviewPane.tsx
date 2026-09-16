/**
 * PreviewPane —— 安全关键组件。
 *
 * 对应 specs/001-atoms-demo/contracts/streaming-events.md 的渲染约束：
 * - sandbox 属性以**常量硬编码**，不接受 props 或任何配置注入
 * - **严禁** allow-same-origin：与 allow-scripts 同时存在时 iframe 将获得与父页面
 *   相同的源，可读取父页面 DOM 与 cookie，等于完全绕过隔离
 * - html 字段是不可信输入（由大模型生成），CSP 已在后端注入
 */
import { useMemo } from 'react';
import { Loader2, MonitorPlay, ShieldCheck } from 'lucide-react';

/**
 * 沙箱属性常量。不要改成变量、props 或配置项。
 * 不含 allow-same-origin，iframe 因此获得不透明源（opaque origin）。
 */
const SANDBOX_ATTR = 'allow-scripts allow-forms';

interface PreviewPaneProps {
  html: string;
  isGenerating: boolean;
  emptyHint?: string;
}

export default function PreviewPane({ html, isGenerating, emptyHint }: PreviewPaneProps) {
  const srcDoc = useMemo(() => html || '', [html]);

  if (!srcDoc) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
        {isGenerating ? (
          <>
            <Loader2 className="h-8 w-8 animate-spin text-sky-400" />
            <p className="text-sm text-slate-400">智能体正在生成你的应用…</p>
            <p className="text-xs text-slate-500">左侧步骤会实时反映当前进展</p>
          </>
        ) : (
          <>
            <div className="rounded-2xl border border-slate-700/60 bg-slate-800/40 p-5">
              <MonitorPlay className="h-9 w-9 text-slate-500" />
            </div>
            <p className="text-sm text-slate-400">
              {emptyHint || '生成完成后，可交互的应用会在这里实时呈现'}
            </p>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-center gap-2 border-b border-slate-800 bg-slate-900/60 px-4 py-2">
        <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
        <span className="text-[11px] text-slate-400">
          隔离沙箱运行中 · sandbox="{SANDBOX_ATTR}" · 无法访问平台数据与凭据
        </span>
      </div>
      <iframe
        title="生成的应用预览"
        className="h-full w-full flex-1 border-0 bg-white"
        sandbox={SANDBOX_ATTR}
        srcDoc={srcDoc}
      />
    </div>
  );
}
