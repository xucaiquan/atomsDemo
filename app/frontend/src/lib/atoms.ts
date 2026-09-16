/**
 * Atoms Demo 平台的后端 API 封装。
 *
 * 对应 specs/001-atoms-demo/contracts/rest-api.md：
 * - 对外一律使用 public_id，响应中不含自增 id
 * - 统一解析错误信封 {"error": {"code", "message"}}，把 message 作为可展示文案抛出
 */
import { createClient } from '@metagptx/web-sdk';

const client = createClient();

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
  summary: Record<string, unknown> | null;
  status: VersionStatus;
  error: string | null;
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
  steps: GenerationStep[];
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
  try {
    const response = await client.apiCall.invoke({
      url,
      method,
      data,
      ...(timeout ? { options: { timeout } } : {}),
    });
    return unwrap<T>(response.data);
  } catch (e) {
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
};
