/**
 * CodeViewer —— 只读源代码视图（FR-009 / T033）。
 *
 * 规格原计划使用 Monaco；为控制产物体积与加载性能，这里用带行号与语法着色的
 * 轻量只读视图实现同等能力（查看源代码），并提供复制。
 */
import { useMemo, useState } from 'react';
import { Check, Copy } from 'lucide-react';

interface CodeViewerProps {
  html: string;
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

export default function CodeViewer({ html }: CodeViewerProps) {
  const [copied, setCopied] = useState(false);
  const lines = useMemo(() => (html ? html.split('\n') : []), [html]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(html);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
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
      <div className="flex shrink-0 items-center justify-between border-b border-slate-800 bg-slate-900/60 px-4 py-2">
        <span className="text-[11px] text-slate-400">
          index.html · {lines.length} 行 · {(html.length / 1024).toFixed(1)} KB
        </span>
        <button
          type="button"
          onClick={handleCopy}
          className="flex items-center gap-1 rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-300 transition-colors hover:border-sky-500/50 hover:text-sky-300"
        >
          {copied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
          {copied ? '已复制' : '复制代码'}
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
