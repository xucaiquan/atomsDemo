/**
 * CodeViewer —— 只读源代码视图（FR-009 / T033 / S5.2）。
 *
 * 规格原计划使用 Monaco；为控制产物体积与加载性能，这里用带行号与语法着色的
 * 轻量只读视图实现同等能力（查看源代码），并提供复制。
 *
 * 工具条展示当前版本号、大小与后端计算的 SHA-256（与预览工具条同源同值），
 * 用于核验「预览所见 == 源码所存」。
 */
import { useMemo, useState } from 'react';
import { AlertCircle, Check, Copy } from 'lucide-react';

interface CodeViewerProps {
  html: string;
  /** 当前展示的版本号。 */
  seq?: number | null;
  /** 后端对存储 HTML 计算的 sha256（S5.2）。 */
  sha256?: string;
}

const KEYWORD_RE =
  /(&lt;\/?)([a-zA-Z][\w-]*)|(&gt;)|("[^"]*")|('(?:[^'\\]|\\.)*')|(\/\/[^\n]*|\/\*[\s\S]*?\*\/)/g;

function escapeHtml(text: string): string {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/** 极简着色：标签名 / 属性字符串 / 注释。避免引入高亮库。 */
function highlight(line: string): string {
  return escapeHtml(line).replace(
    KEYWORD_RE,
    (_match, tagOpen, tagName, tagClose, dq, sq, comment) => {
      if (tagOpen && tagName)
        return `<span class="text-slate-500">${tagOpen}</span><span class="text-sky-400">${tagName}</span>`;
      if (tagClose) return `<span class="text-slate-500">${tagClose}</span>`;
      if (dq) return `<span class="text-amber-300">${dq}</span>`;
      if (sq) return `<span class="text-amber-300">${sq}</span>`;
      if (comment) return `<span class="italic text-slate-600">${comment}</span>`;
      return _match;
    },
  );
}

type CopyState = 'idle' | 'copied' | 'failed';

/**
 * 复制文本到剪贴板，带降级路径。
 *
 * `navigator.clipboard` 只在安全上下文（https / localhost）下存在，且在被嵌入的
 * iframe 中还可能因缺少 clipboard-write 权限而 reject。此前实现只调用它、失败时
 * 静默吞掉异常，所以在这些环境里按钮点了完全没反应——这正是「复制代码按钮不生效」
 * 的成因。这里先试异步 API，不可用或抛错时回退到 execCommand('copy')，
 * 两条路都失败才返回 false，由调用方给出可见的失败提示。
 */
async function copyText(text: string): Promise<boolean> {
  if (!text) return false;

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // 落到下面的 execCommand 降级路径
    }
  }

  try {
    const area = document.createElement('textarea');
    area.value = text;
    // 必须在文档里且可聚焦才能被 execCommand 选中；同时避免滚动跳动与可见闪烁。
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.top = '0';
    area.style.left = '0';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.focus();
    area.select();
    area.setSelectionRange(0, text.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

export default function CodeViewer({ html, seq, sha256 }: CodeViewerProps) {
  const [copyState, setCopyState] = useState<CopyState>('idle');
  const [shaCopyState, setShaCopyState] = useState<CopyState>('idle');
  const lines = useMemo(() => (html ? html.split('\n') : []), [html]);

  const runCopy = async (
    text: string,
    setState: (next: CopyState) => void,
  ) => {
    const ok = await copyText(text);
    setState(ok ? 'copied' : 'failed');
    setTimeout(() => setState('idle'), ok ? 1600 : 2600);
  };

  const handleCopy = () => runCopy(html, setCopyState);

  const handleCopySha = () => {
    if (!sha256) return Promise.resolve();
    return runCopy(sha256, setShaCopyState);
  };

  if (!html) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-slate-500">
        生成完成后，这里会展示源代码
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-slate-800 bg-slate-900/60 px-4 py-2">
        <span className="flex min-w-0 flex-wrap items-center gap-2 text-[11px] text-slate-400">
          <span>
            index.html{seq != null ? ` · v${seq}` : ''} · {lines.length} 行 ·{' '}
            {(html.length / 1024).toFixed(1)} KB
          </span>
          {sha256 && (
            <button
              type="button"
              onClick={() => void handleCopySha()}
              title="点击复制完整 SHA-256（与预览同一版本内容哈希）"
              className="truncate rounded border border-slate-700 bg-slate-950/60 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 transition-colors hover:border-sky-500/50 hover:text-sky-300"
            >
              SHA-256{' '}
              {shaCopyState === 'copied'
                ? '已复制 ✓'
                : shaCopyState === 'failed'
                  ? '复制失败，请手动选中'
                  : `${sha256.slice(0, 16)}…`}
            </button>
          )}
        </span>
        <button
          type="button"
          onClick={() => void handleCopy()}
          className="flex items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-300 transition-colors hover:border-sky-500/50 hover:text-sky-300"
        >
          {copyState === 'copied' ? (
            <Check className="h-3 w-3 text-emerald-400" />
          ) : copyState === 'failed' ? (
            <AlertCircle className="h-3 w-3 text-amber-400" />
          ) : (
            <Copy className="h-3 w-3" />
          )}
          {copyState === 'copied'
            ? '已复制'
            : copyState === 'failed'
              ? '复制失败'
              : '复制代码'}
        </button>
      </div>
      <div className="flex-1 overflow-auto bg-slate-950/80 font-mono text-[12px] leading-5">
        <table className="w-full border-collapse">
          <tbody>
            {lines.map((line, index) => (
              <tr key={index}>
                <td className="w-12 select-none border-r border-slate-800/60 px-2 text-right align-top text-slate-600">
                  {index + 1}
                </td>
                <td
                  className="whitespace-pre-wrap break-all px-3 align-top text-slate-300"
                  dangerouslySetInnerHTML={{ __html: highlight(line) || '&nbsp;' }}
                />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
