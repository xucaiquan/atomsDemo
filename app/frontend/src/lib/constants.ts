/** prompt 长度约束提示，与后端 schemas 保持一致。 */
export const PROMPT_MAX_LEN_HINT = 2000;

/** 相对时间格式化，用于项目列表。 */
export function formatRelative(iso: string | null): string {
  if (!iso) return '';
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return '';
  const diff = Date.now() - time;
  const minute = 60_000;
  if (diff < minute) return '刚刚';
  if (diff < 60 * minute) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < 24 * 60 * minute) return `${Math.floor(diff / (60 * minute))} 小时前`;
  if (diff < 7 * 24 * 60 * minute) return `${Math.floor(diff / (24 * 60 * minute))} 天前`;
  return new Date(iso).toLocaleDateString('zh-CN');
}

// 设计文档 2026-09-20 S2.1：删除可伪造的 getOwnerKey()。
// 归属键改由服务端派生（dependencies/owner.py），前端只经 X-Atoms-Anon
// 头回传后端签发的匿名标识（见 lib/atoms.ts）。
