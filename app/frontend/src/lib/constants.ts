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

/** 从 localStorage 读取（或生成）浏览器会话标识，对应 spec「无账号体系」假设。 */
export function getOwnerKey(): string {
  try {
    let key = localStorage.getItem('atoms_demo_owner');
    if (!key) {
      key = `anon-${Math.random().toString(36).slice(2, 10)}-${Date.now().toString(36)}`;
      localStorage.setItem('atoms_demo_owner', key);
    }
    return key;
  } catch {
    return 'anon';
  }
}
