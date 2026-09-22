/**
 * Atoms Demo 平台的后端 API 封装。
 *
 * 对应 specs/001-atoms-demo/contracts/rest-api.md 与设计文档 2026-09-20（S1/S2/S5）：
 * - 对外一律使用 public_id，响应中不含自增 id
 * - 统一解析错误信封 {"error": {"code", "message"}}，把 message 作为可展示文案抛出
 * - **匿名身份双通道**：每次请求携带 `X-Atoms-Anon`（localStorage 持久化），
 *   并持久化后端响应里的 `anon_key`。归属键由服务端派生，前端不再自造
 *   owner_key（旧 getOwnerKey() 已删除，客户端无法伪造身份）。
 */
import { createClient } from '@metagptx/web-sdk';

const client = createClient();

/** 与后端 dependencies/owner.py 常量一致。 */
const ANON_HEADER = 'X-Atoms-Anon';
const ANON_STORAGE_KEY = 'atoms_anon_key';
/** nonce(32) + '.' + sig(16) = 49，超长即非服务端签发值，不持久化。 */
const ANON_MAX_LEN = 49;

function getStoredAnonKey(): string | null {
  try {
    const raw = localStorage.getItem(ANON_STORAGE_KEY);
    return raw && raw.length <= ANON_MAX_LEN ? raw : null;
  } catch {
    return null;
  }
}

/** 后端签发的匿名标识（响应体 anon_key）落盘，下次请求经头通道回传。 */
function persistAnonKey(payload: unknown): void {
  const raw = (payload as { anon_key?: unknown })?.anon_key;
  if (typeof raw === 'string' && raw && raw.length <= ANON_MAX_LEN) {
    try {
      localStorage.setItem(ANON_STORAGE_KEY, raw);
    } catch {
      /* 隐私模式下忽略 */
    }
  }
}

/**
 * 只清 localStorage 里的匿名标识副本。
 *
 * **这不等于登出。** 身份的主通道是 HttpOnly cookie，前端 JS 原理上读不到也
 * 删不掉它；清掉 localStorage 只让请求头通道不再携带旧标识，请求到达服务端时
 * 仍会带上旧 cookie，于是身份还是旧的。旧代码把本函数当登出用，用户看到的现象
 * 就是「登出无效」。要真正换身份，必须走 `atomsApi.logout()`（服务端下发新身份）。
 *
 * 保留本函数是给「登录态切换」用的：登录身份的优先级高于匿名身份，此时清掉
 * 匿名副本可以避免与登录身份混用同一份浏览器存储。
 */
export function clearAnonKey(): void {
  try {
    localStorage.removeItem(ANON_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

/** 生成步骤的五态（cancelled：用户主动停止生成）。 */
export type StepStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

/** 版本状态，与 data-model.md 的状态机一致（cancelled 为用户取消终态）。 */
export type VersionStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled';

export interface GenerationStep {
  seq: number;
  name: string;
  status: StepStatus;
  output?: string;
  started_at?: string | null;
  ended_at?: string | null;
}

export interface ProjectBrief {
  public_id: string;
  title: string;
  created_at: string | null;
  updated_at: string | null;
  version_count: number;
  latest_status: VersionStatus | null;
  is_demo?: boolean;
}

export interface VersionBrief {
  seq: number;
  prompt: string;
  status: VersionStatus;
  error: string | null;
  /**
   * 上游/平台失败分类。刷新落地后前端只拿得到项目详情里的版本列表，若这里没有
   * 分类，就无法判断上一次失败是否属于超时，也就谈不上「刷新后接着重试」。
   */
  error_type?: string | null;
  duration_ms: number | null;
  created_at: string | null;
  steps: GenerationStep[];
}

export interface ConversationMessage {
  role: 'user' | 'assistant';
  content: string;
  version_seq: number | null;
  created_at: string | null;
}

export interface ProjectDetail extends ProjectBrief {
  versions: VersionBrief[];
  messages: ConversationMessage[];
}

export interface VersionDetail {
  seq: number;
  prompt: string;
  html: string;
  /** 后端对存储 HTML 计算的 sha256（S5.2）：预览与源码工具条渲染同一个值。 */
  html_sha256: string;
  summary: Record<string, unknown> | null;
  status: VersionStatus;
  error: string | null;
  /**
   * 失败分类（S3.5）。两类含义不同，界面文案必须区分开（FR-017）：
   * - 上游故障：auth / rate_limit / timeout / upstream_5xx / empty / truncated / unknown
   * - 平台侧止损：budget_exhausted（超出整体时间预算，重试同样会超，需精简需求）
   * - 被中断：interrupted（服务重启或连接断开后的陈旧回收）
   */
  error_type?: string | null;
  duration_ms: number | null;
  created_at: string | null;
}

/**
 * 生成受理响应。
 *
 * 后端三阶段流水线耗时远超网关 120s 代理读超时，generate 接口改为异步受理：
 * 毫秒级返回 202 + 版本号，前端通过 getVersionSteps 轮询真实进度与终态。
 */
export interface GenerateAccepted {
  status: 'accepted';
  version_seq: number;
  steps: GenerationStep[];
  step_names: string[];
}

/** 轮询快照：版本终态 + 实时步骤。 */
export interface StepsSnapshot {
  version_seq: number;
  status: VersionStatus;
  error: string | null;
  /** 这一轮的原始描述：超时重试弹窗据此用原描述重新生成，无需用户重打。 */
  prompt?: string;
  /** 失败分类（S3.5），后端 steps 接口从 summary 派生。 */
  error_type?: string | null;
  steps: GenerationStep[];
}

/** 回滚响应（S5.1「回滚即新版本」）。 */
export interface RestoreResult {
  restored: boolean;
  version_seq: number;
  restored_from: number;
}

/** 三阶段步骤名，前端在提交瞬间即用它本地渲染骨架，不等任何网络往返。 */
export const STEP_NAMES = ['需求分析', '结构设计', '代码生成'] as const;

/** 构造本地步骤骨架（SC-002：3 秒内首个可见反馈的实现基础）。 */
export function buildLocalSteps(): GenerationStep[] {
  return STEP_NAMES.map((name, index) => ({
    seq: index + 1,
    name,
    status: index === 0 ? 'running' : 'pending',
  }));
}

/**
 * 统一解析后端错误信封，抛出面向用户的可读文案。
 *
 * 后端把业务错误以 `{"error": {code, message}}` 返回，这里把 message 提取成
 * Error.message，前端可直接展示，不暴露堆栈或内部路径。
 */
function toReadableError(raw: unknown): Error {
  const err = raw as {
    data?: { error?: { code?: string; message?: string }; detail?: string };
    response?: { data?: { error?: { code?: string; message?: string }; detail?: string } };
    message?: string;
  };
  const envelope = err?.data?.error || err?.response?.data?.error;
  if (envelope?.message) {
    const error = new Error(envelope.message);
    (error as Error & { code?: string }).code = envelope.code;
    return error;
  }
  const detail = err?.data?.detail || err?.response?.data?.detail;
  if (detail) return new Error(detail);
  return new Error(err?.message || '请求失败，请稍后重试');
}

/** 后端返回 2xx 但体内含错误信封时也要转成异常。 */
function unwrap<T>(payload: unknown): T {
  const body = payload as { error?: { code?: string; message?: string } };
  if (body?.error?.message) {
    const error = new Error(body.error.message);
    (error as Error & { code?: string }).code = body.error.code;
    throw error;
  }
  return payload as T;
}

async function invoke<T>(
  url: string,
  method: 'GET' | 'POST' | 'DELETE',
  data: Record<string, unknown> = {},
  timeout?: number,
): Promise<T> {
  const anonKey = getStoredAnonKey();
  const headers: Record<string, string> = {};
  if (anonKey) headers[ANON_HEADER] = anonKey;

  try {
    const response = await client.apiCall.invoke({
      url,
      method,
      data,
      options: {
        ...(Object.keys(headers).length ? { headers } : {}),
        ...(timeout ? { timeout } : {}),
      },
    });
    // 失败信封也可能带 anon_key（后端保证新访客第一次请求即固化身份）
    persistAnonKey(response.data);
    return unwrap<T>(response.data);
  } catch (e) {
    const err = e as { data?: { anon_key?: unknown }; response?: { data?: { anon_key?: unknown } } };
    persistAnonKey(err?.data ?? err?.response?.data);
    throw toReadableError(e);
  }
}

export const atomsApi = {
  /** 项目列表，按 updated_at 倒序，不含 html。 */
  async listProjects(): Promise<ProjectBrief[]> {
    const data = await invoke<{ projects: ProjectBrief[] }>(
      '/api/v1/atoms/projects',
      'GET',
    );
    return data.projects || [];
  },

  /** 创建空项目，title 省略时由后端生成占位名。 */
  createProject(title?: string): Promise<ProjectBrief> {
    return invoke<ProjectBrief>('/api/v1/atoms/projects', 'POST', {
      title: title || null,
    });
  },

  /** 项目详情：版本列表（含步骤）与对话记录，不含 html。 */
  getProject(publicId: string): Promise<ProjectDetail> {
    return invoke<ProjectDetail>(`/api/v1/atoms/projects/${publicId}`, 'GET');
  },

  /** 单个版本完整内容，仅此接口返回 html。 */
  getVersion(publicId: string, seq: number): Promise<VersionDetail> {
    return invoke<VersionDetail>(
      `/api/v1/atoms/projects/${publicId}/versions/${seq}`,
      'GET',
    );
  },

  /** 轮询某版本的实时步骤状态与版本终态。 */
  getVersionSteps(publicId: string, seq: number): Promise<StepsSnapshot> {
    return invoke<StepsSnapshot>(
      `/api/v1/atoms/projects/${publicId}/versions/${seq}/steps`,
      'GET',
    );
  },

  /** 删除项目（级联删除版本、消息与步骤）。 */
  deleteProject(publicId: string): Promise<{ deleted: boolean }> {
    return invoke(`/api/v1/atoms/projects/${publicId}`, 'DELETE');
  },

  /**
   * 受理三阶段生成（异步）。
   *
   * 后端只做校验 + 落库后立即返回 202（毫秒级），三阶段在后台任务中执行，
   * 彻底避开网关 120s 代理读超时。前端拿到 version_seq 后轮询 getVersionSteps。
   */
  generate(publicId: string, prompt: string): Promise<GenerateAccepted> {
    return invoke<GenerateAccepted>(
      `/api/v1/atoms/projects/${publicId}/generate`,
      'POST',
      { prompt },
    );
  },

  /**
   * 取消进行中的生成（中断任务能力）。
   *
   * 后端立即把版本与活跃步骤落库为 cancelled 并中断进程内后台任务；
   * 已成功的旧版本不受影响，用户输入的需求保留可继续提交新要求。
   */
  cancelGeneration(
    publicId: string,
    seq: number,
  ): Promise<{ status: string; version_seq: number }> {
    return invoke(
      `/api/v1/atoms/projects/${publicId}/versions/${seq}/cancel`,
      'POST',
    );
  },

  /**
   * 回滚到指定历史版本（S5.1，「回滚即新版本」）。
   *
   * 后端创建 max(seq)+1 的新版本逐字节复制目标内容，原版本全部保留；
   * 下一轮生成自动以回滚结果为增量基线。演示项目返回 409。
   */
  restoreVersion(publicId: string, seq: number): Promise<RestoreResult> {
    return invoke(
      `/api/v1/atoms/projects/${publicId}/versions/${seq}/restore`,
      'POST',
    );
  },

  /**
   * 登出：换取全新匿名身份，返还服务端签发的新 `anon_key`（失败时为 null）。
   *
   * 必须打后端——匿名 cookie 是 HttpOnly 的，前端 JS 删不掉它（见 clearAnonKey
   * 的说明）。服务端会覆盖旧 cookie 并签发新身份，双通道一并下发。
   *
   * 顺序有意为之：**先清本地、再发请求**。这样即便请求失败，请求头通道也不会
   * 继续携带旧标识，不会退化成「旧身份被两个通道同时续用」；此时 cookie 通道
   * 仍归服务端管，前端无能为力，属于已知边界。
   *
   * 新身份由 invoke 内的 persistAnonKey 落盘（响应体同样带 anon_key），
   * 于是请求头通道与 cookie 通道保持一致，都指向登出后的新身份。
   */
  async logout(): Promise<string | null> {
    clearAnonKey();
    const data = await invoke<{ status: string; anon_key?: string }>(
      '/api/v1/atoms/session/logout',
      'POST',
    );
    return data.anon_key ?? null;
  },
};
