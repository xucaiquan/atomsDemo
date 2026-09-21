/**
 * Workspace —— 主工作区（三栏布局）。
 *
 * 核心流程（对应 specs/001-atoms-demo 与设计文档 2026-09-20 S2/S5）：
 * 1. 提交瞬间本地构造 3 条步骤骨架并渲染，不等任何网络往返（SC-002）
 * 2. 生成期间轮询步骤状态，把真实进程映射到步骤流转（US4 / FR-008）
 * 3. 成功后渲染进沙箱 iframe；失败时保留用户输入并给出可读中文提示（FR-011）
 * 4. 认证三态（loading/authenticated/anonymous）：登录/登出切换身份时清空
 *    全部工作区状态，杜绝跨身份数据残留（S2.4）
 * 5. 预览与源码展示同一版本的大小与 SHA-256；回滚走 restore 生成新版本（S5）
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  Bot,
  Code2,
  History,
  Loader2,
  LogIn,
  LogOut,
  MessageSquare,
  MonitorPlay,
  Plus,
  Sparkles,
  Wand2,
} from 'lucide-react';

import AgentSteps from '@/components/AgentSteps';
import CodeViewer from '@/components/CodeViewer';
import ConversationPanel from '@/components/ConversationPanel';
import PreviewPane from '@/components/PreviewPane';
import ProjectList from '@/components/ProjectList';
import PromptInput from '@/components/PromptInput';
import VersionSwitcher from '@/components/VersionSwitcher';
import { Button } from '@/components/ui/button';
import { useAuth } from '@/contexts/AuthContext';
import {
  atomsApi,
  buildLocalSteps,
  type ConversationMessage,
  type GenerationStep,
  type ProjectBrief,
  type ProjectDetail,
  type StepsSnapshot,
  type VersionBrief,
} from '@/lib/atoms';

const POLL_INTERVAL_MS = 2500;

/**
 * 前端等待后台生成终态的最长时间。略大于后端陈旧任务恢复阈值（10 分钟），
 * 超时后后端会把卡住的版本标记为 failed，前端同步展示可读原因。
 */
const GENERATION_POLL_TIMEOUT_MS = 12 * 60 * 1000;

/** 预览失败原因条里展示的上游错误分类（S3.5 error_type）。 */
const ERROR_TYPE_LABELS: Record<string, string> = {
  auth: '上游鉴权失败',
  rate_limit: '上游限流（已自动重试）',
  timeout: '上游响应超时',
  empty: '上游返回空内容',
  truncated: '输出被截断',
  // 平台侧止损：不是上游故障，措辞必须与 timeout 明确区分（FR-017）。
  budget_exhausted: '超出时间预算',
  // 陈旧回收写入的分类：服务重启或连接断开导致的中断。
  interrupted: '生成被中断',
  unknown: '未知错误',
};

/**
 * 每一类失败对应的**下一步动作**（FR-017：失败说明必须给出可理解的原因与出口）。
 *
 * 「超出时间预算」与「上游不可用」的区别必须落到用户动作上：前者重试同样会
 * 超预算，必须精简需求；后者是上游自己会恢复的，稍后重试即可。只把两种说法
 * 并列展示而不说清该做什么，用户仍会一直重试一个注定失败的请求。
 */
const ERROR_TYPE_HINTS: Record<string, string> = {
  budget_exhausted: '可精简描述或拆成两步后再试，重试同样的描述很可能再次超时',
  interrupted: '重新提交即可继续，之前的成功版本不受影响',
  auth: '需要管理员处理，重试无用',
  truncated: '描述里包含的内容过多，精简后重试',
  empty: '直接重新提交即可，通常是上游偶发空响应',
  rate_limit: '上游繁忙，稍后重试即可',
  timeout: '上游暂时不响应，稍后重试即可',
  unknown: '稍后重试即可',
};

/**
 * 上次打开的项目标识（FR-026：刷新后回到原来的地方）。
 *
 * 只存 public_id，不存项目内容——内容必须重新从服务端取，避免拿本地副本
 * 冒充服务端状态。归属校验仍在服务端：恢复时若该 id 不属于当前身份（换了
 * 身份、登出后又刷新），详情请求会 404，此时静默丢弃即可。
 */
const LAST_PROJECT_KEY = 'atoms_last_project';

function readLastProjectId(): string | null {
  try {
    return localStorage.getItem(LAST_PROJECT_KEY);
  } catch {
    return null;
  }
}

function writeLastProjectId(publicId: string | null): void {
  try {
    if (publicId) localStorage.setItem(LAST_PROJECT_KEY, publicId);
    else localStorage.removeItem(LAST_PROJECT_KEY);
  } catch {
    /* 隐私模式下忽略 */
  }
}

export default function Index() {
  const { user, status: authStatus, login, logout } = useAuth();

  // ---------------- 项目与详情 ----------------
  const [projects, setProjects] = useState<ProjectBrief[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ProjectDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // ---------------- 生成状态 ----------------
  const [prompt, setPrompt] = useState('');
  const [isGenerating, setIsGenerating] = useState(false);
  const [steps, setSteps] = useState<GenerationStep[]>([]);
  const [failedMessage, setFailedMessage] = useState<string | null>(null);
  const [failedType, setFailedType] = useState<string | null>(null);
  // 本次提交的描述：失败后的一键重试据此复用（输入框在受理时已被清空）
  const [submittedPrompt, setSubmittedPrompt] = useState('');
  const [html, setHtml] = useState('');
  // 当前展示版本的 SHA-256（S5.2：预览与源码工具条渲染同一个值）
  const [htmlSha256, setHtmlSha256] = useState('');
  const [activeSeq, setActiveSeq] = useState<number | null>(null);
  const [view, setView] = useState<'preview' | 'code'>('preview');

  // ---------------- 数据加载 ----------------
  const refreshProjects = useCallback(async () => {
    try {
      const list = await atomsApi.listProjects();
      setProjects(list);
      return list;
    } catch (e) {
      toast.error((e as Error).message || '项目列表加载失败');
      return [];
    } finally {
      setProjectsLoading(false);
    }
  }, []);

  /** 清空工作区所有与身份绑定的状态（S2.4：登录/登出/新项目共用）。 */
  const resetWorkspace = useCallback(() => {
    stopPollingRef.current?.();
    setIsGenerating(false);
    generatingSeqRef.current = null;
    resumedRef.current = null;
    setActiveId(null);
    setDetail(null);
    setProjects([]);
    setHtml('');
    setHtmlSha256('');
    setSteps([]);
    setActiveSeq(null);
    setFailedMessage(null);
    setFailedType(null);
    setPrompt('');
    setView('preview');
  }, []);

  const openProject = useCallback(async (publicId: string) => {
    setActiveId(publicId);
    setDetailLoading(true);
    setFailedMessage(null);
    setFailedType(null);
    // 切换项目先清空旧 HTML，避免旧内容挂在新项目名下（S5.3 过渡态）
    setHtml('');
    setHtmlSha256('');
    try {
      const data = await atomsApi.getProject(publicId);
      setDetail(data);
      // 默认打开最近一个成功版本（US2：历史找回后原样重现）
      const latest = [...data.versions].reverse().find((v) => v.status === 'succeeded');
      if (latest) {
        setActiveSeq(latest.seq);
        try {
          const full = await atomsApi.getVersion(publicId, latest.seq);
          setHtml(full.html || '');
          setHtmlSha256(full.html_sha256 || '');
        } catch {
          setHtml('');
          setHtmlSha256('');
        }
        setSteps(latest.steps);
      } else {
        setActiveSeq(null);
        setHtml('');
        setHtmlSha256('');
        setSteps([]);
      }
    } catch (e) {
      toast.error((e as Error).message || '项目加载失败');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshProjects();
  }, [refreshProjects]);

  // ---------------- 身份切换清态（S2.4） ----------------
  // auth 解析完成（loading → authenticated/anonymous）后，若身份与上次不同，
  // 清空全部工作区状态并按新身份重新拉列表。me() 未返回前不做任何假定。
  const identityRef = useRef<string | null>(null);
  useEffect(() => {
    if (authStatus === 'loading') return;
    const key = authStatus === 'authenticated' && user ? `u:${user.id}` : 'anon';
    if (identityRef.current === null) {
      identityRef.current = key;
      return;
    }
    if (identityRef.current !== key) {
      identityRef.current = key;
      resetWorkspace();
      void refreshProjects();
    }
  }, [authStatus, user, resetWorkspace, refreshProjects]);

  const handleLogout = useCallback(async () => {
    // 两件事都得做，缺一不可：
    // ① atomsApi.logout() 轮换**浏览器侧的匿名身份**。不能省——匿名 cookie 是
    //    HttpOnly 的，前端只能清掉 localStorage 里那份副本，cookie 原样留着，
    //    请求到达服务端时身份还是旧的（用户看到「登出无效，还看得到刚才的东西」）。
    // ② useAuth().logout() 清掉登录态。
    try {
      await atomsApi.logout();
    } catch {
      // 匿名身份轮换失败不能阻断登出：①失败时仍要完成 ②，否则用户困在登录态。
    }
    await logout();
    // logout() 会把状态切到 anonymous，上面的身份 effect 负责清态与重拉
  }, [logout]);

  // ---------------- 刷新恢复：回到上次打开的项目（FR-026） ----------------
  // 刷新后 activeId 初始为 null，工作区是空的——用户得自己再从列表里找回来。
  // 这里在身份与项目列表都就绪后自动载入上次打开的项目；若该项目不属于当前
  // 身份（登出过 / 换了身份）或已被删除，列表里就找不到它，直接丢弃记录。
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current || activeId) return;
    if (authStatus === 'loading' || projectsLoading) return;
    const stored = readLastProjectId();
    if (!stored) return;
    restoredRef.current = true;
    if (!projects.some((p) => p.public_id === stored)) {
      // 列表里没有：不属于当前身份或已被删除。丢弃记录，避免每次渲染重试。
      writeLastProjectId(null);
      return;
    }
    void openProject(stored);
  }, [activeId, authStatus, projectsLoading, projects, openProject]);

  // 把「当前打开的项目」记到浏览器侧，供刷新后恢复（FR-026）。
  // activeId 置空（resetWorkspace）时会一并删除记录——换身份后不该再恢复
  // 上一个身份的项目。
  useEffect(() => {
    writeLastProjectId(activeId);
  }, [activeId]);

  // ---------------- 版本切换（US3 / FR-007 / S5.3） ----------------
  const switchVersion = useCallback(
    async (seq: number) => {
      if (!activeId) return;
      setActiveSeq(seq);
      setView('preview');
      // 先清空旧 HTML：加载中不会把旧版本内容标成新版本（过渡态修复）
      setHtml('');
      setHtmlSha256('');
      setFailedMessage(null);
      setFailedType(null);
      try {
        const full = await atomsApi.getVersion(activeId, seq);
        setHtml(full.html || '');
        setHtmlSha256(full.html_sha256 || '');
        if (full.status === 'failed') {
          setFailedMessage(full.error);
          setFailedType(full.error_type || null);
          // 版本里持久化着这一轮的原始描述（FR-019）：刷新后重新打开项目时，
          // 「用原描述重新生成」据它可用——否则重试入口会因记忆丢失而失效。
          setSubmittedPrompt(full.prompt || '');
        }
        const version = detail?.versions.find((v) => v.seq === seq);
        if (version) setSteps(version.steps);
      } catch (e) {
        toast.error((e as Error).message || '版本内容加载失败');
      }
    },
    [activeId, detail],
  );

  // ---------------- 回滚（S5.1：回滚即新版本） ----------------
  const handleRestore = useCallback(
    async (seq: number) => {
      if (!activeId) return;
      try {
        const res = await atomsApi.restoreVersion(activeId, seq);
        toast.success(`已回滚 v${seq}，生成新版本 v${res.version_seq}`);
        const data = await atomsApi.getProject(activeId);
        setDetail(data);
        const restored = data.versions.find((v) => v.seq === res.version_seq);
        if (restored) setSteps(restored.steps);
        setActiveSeq(res.version_seq);
        setHtml('');
        setHtmlSha256('');
        const full = await atomsApi.getVersion(activeId, res.version_seq);
        setHtml(full.html || '');
        setHtmlSha256(full.html_sha256 || '');
      } catch (e) {
        toast.error((e as Error).message || '回滚失败');
      }
    },
    [activeId],
  );

  // ---------------- 后台生成轮询（异步受理 + 终态驱动） ----------------
  // 后端 generate 毫秒级返回 202 受理，三阶段在 asyncio 后台任务中执行；
  // 前端只轮询轻量的 steps 接口观察真实进度，直到 succeeded / failed。
  const pollTimerRef = useRef<number | null>(null);
  // 当前正在生成的版本号，供「停止生成」调用取消接口时使用
  const generatingSeqRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // 供 resetWorkspace（早于定义处使用）安全调用
  const stopPollingRef = useRef<(() => void) | null>(null);
  stopPollingRef.current = stopPolling;

  /** 轮询直到版本进入终态；网络抖动不中断，超时按失败处理。 */
  const awaitGeneration = useCallback(
    (publicId: string, seq: number) =>
      new Promise<StepsSnapshot>((resolve) => {
        const deadline = Date.now() + GENERATION_POLL_TIMEOUT_MS;
        const tick = async () => {
          let snapshot: StepsSnapshot | null = null;
          try {
            snapshot = await atomsApi.getVersionSteps(publicId, seq);
            if (snapshot.steps.length) setSteps(snapshot.steps);
          } catch {
            /* steps 尚未落库或网络抖动，忽略并继续轮询 */
          }
          if (
            snapshot &&
            (snapshot.status === 'succeeded' ||
              snapshot.status === 'failed' ||
              snapshot.status === 'cancelled')
          ) {
            pollTimerRef.current = null;
            resolve(snapshot);
            return;
          }
          if (Date.now() > deadline) {
            pollTimerRef.current = null;
            resolve({
              version_seq: seq,
              status: 'failed',
              error: '生成等待超时，请重试（你的描述已保留）',
              steps: [],
            });
            return;
          }
          pollTimerRef.current = window.setTimeout(tick, POLL_INTERVAL_MS);
        };
        void tick();
      }),
    [],
  );

  /** 终态落地：成功取 HTML 渲染，失败展示可读原因，并刷新持久化数据。 */
  const finishGeneration = useCallback(async (publicId: string, snapshot: StepsSnapshot) => {
    if (snapshot.status === 'succeeded') {
      try {
        const full = await atomsApi.getVersion(publicId, snapshot.version_seq);
        setHtml(full.html || '');
        setHtmlSha256(full.html_sha256 || '');
        setActiveSeq(full.seq);
        setFailedMessage(null);
        setFailedType(null);
        toast.success('生成完成，在右侧直接体验');
      } catch {
        setFailedMessage('生成结果加载失败，可切换版本重试');
      }
    } else if (snapshot.status === 'cancelled') {
      // 用户主动停止：不算失败，展示取消态步骤，旧版本保持可用
      setFailedMessage(null);
      setFailedType(null);
      if (snapshot.steps.length) setSteps(snapshot.steps);
      toast.info('已停止本次生成，之前的版本不受影响');
    } else {
      setFailedMessage(snapshot.error || '生成失败，你的描述已保留');
      setFailedType(snapshot.error_type || null);
      toast.error(snapshot.error || '生成失败，你的描述已保留');
      if (snapshot.steps.length) setSteps(snapshot.steps);
    }
    try {
      const [list, data] = await Promise.all([
        atomsApi.listProjects(),
        atomsApi.getProject(publicId),
      ]);
      setProjects(list);
      setDetail(data);
    } catch {
      /* 刷新失败不影响已展示的生成结果 */
    }
  }, []);

  // ---------------- 提交生成（US1 + US3，异步受理） ----------------

  /**
   * 以给定描述跑一次生成。与 `prompt` 输入框解耦，供「重新生成」入口复用
   * （FR-017 的重试入口：重试必须能带上**原描述**，而不是要求用户重打一遍）。
   */
  const runGeneration = useCallback(
    async (text: string) => {
      if (!text || isGenerating) return;
      setFailedMessage(null);
      setFailedType(null);
      // 记住本次提交的描述：失败后据此提供重试入口（FR-017/FR-019）
      setSubmittedPrompt(text);
      setIsGenerating(true);
      setView('preview');

      // ① 提交瞬间本地渲染步骤骨架 —— 不等待任何网络往返（SC-002）
      setSteps(buildLocalSteps());

      try {
        // ② 无项目时先创建空项目
        let publicId = activeId;
        if (!publicId) {
          const created = await atomsApi.createProject();
          publicId = created.public_id;
          setActiveId(publicId);
        }

        // ③ 受理生成：后端只做校验 + 落库，毫秒级返回 202 + version_seq。
        const accepted = await atomsApi.generate(publicId, text);
        generatingSeqRef.current = accepted.version_seq;
        setPrompt('');
        if (accepted.steps?.length) setSteps(accepted.steps);

        // ④ 轮询后台任务直到终态（含 cancelled），再取 HTML 渲染
        const snapshot = await awaitGeneration(publicId, accepted.version_seq);
        setIsGenerating(false);
        generatingSeqRef.current = null;
        await finishGeneration(publicId, snapshot);
      } catch (e) {
        stopPolling();
        setIsGenerating(false);
        const error = e as Error & { code?: string };
        // 受理前的校验错误（400/403/404/409）：输入保留（FR-011），骨架回退失败态
        setFailedMessage(error.message || '生成失败，请稍后重试');
        setFailedType(null);
        toast.error(error.message || '生成失败，你的描述已保留');
        setSteps((prev) =>
          prev.map((s, i) =>
            s.status === 'running' || i === 0
              ? { ...s, status: 'failed', output: error.message }
              : s,
          ),
        );
      }
    },
    [isGenerating, activeId, awaitGeneration, finishGeneration, stopPolling],
  );

  const handleSubmit = useCallback(() => {
    void runGeneration(prompt.trim());
  }, [prompt, runGeneration]);

  /** 失败后的一键重试：原描述直接复用（FR-017/FR-019）。 */
  const handleRetry = useCallback(() => {
    void runGeneration(submittedPrompt);
  }, [submittedPrompt, runGeneration]);

  // ---------------- 刷新恢复：接回进行中的后台生成 ----------------
  // 页面刷新 / 浏览器关闭后重新打开时，若项目存在 pending/running 版本
  // （后端 asyncio 任务仍在执行），自动接回轮询直到终态，无需用户重新提交。
  const resumedRef = useRef<string | null>(null);

  useEffect(() => {
    if (!detail || isGenerating) return;
    const activeVersion = detail.versions.find(
      (v) => v.status === 'pending' || v.status === 'running',
    );
    if (!activeVersion) return;
    const key = `${detail.public_id}:${activeVersion.seq}`;
    if (resumedRef.current === key) return;
    resumedRef.current = key;
    generatingSeqRef.current = activeVersion.seq;
    setIsGenerating(true);
    setSteps(activeVersion.steps?.length ? activeVersion.steps : buildLocalSteps());
    void (async () => {
      const snapshot = await awaitGeneration(detail.public_id, activeVersion.seq);
      setIsGenerating(false);
      generatingSeqRef.current = null;
      await finishGeneration(detail.public_id, snapshot);
    })();
  }, [detail, isGenerating, awaitGeneration, finishGeneration]);

  // 组件卸载时清理轮询定时器
  useEffect(() => stopPolling, [stopPolling]);

  // ---------------- 停止生成（任务中断能力） ----------------
  const handleStop = useCallback(async () => {
    const seq = generatingSeqRef.current;
    if (!activeId || seq == null) return;
    try {
      await atomsApi.cancelGeneration(activeId, seq);
    } catch (e) {
      // 版本可能恰好已完成：轮询会以真实终态收尾，这里仅提示
      toast.error((e as Error).message || '停止失败');
    }
  }, [activeId]);

  // ---------------- 新建项目 ----------------
  const handleNewProject = useCallback(() => {
    setActiveId(null);
    setDetail(null);
    setHtml('');
    setHtmlSha256('');
    setSteps([]);
    setActiveSeq(null);
    setFailedMessage(null);
    setFailedType(null);
    setPrompt('');
    setView('preview');
  }, []);

  // ---------------- 删除项目 ----------------
  const handleDelete = useCallback(
    async (publicId: string) => {
      try {
        await atomsApi.deleteProject(publicId);
        toast.success('项目已删除');
        if (activeId === publicId) handleNewProject();
        void refreshProjects();
      } catch (e) {
        toast.error((e as Error).message || '删除失败');
      }
    },
    [activeId, handleNewProject, refreshProjects],
  );

  const versions: VersionBrief[] = detail?.versions ?? [];
  const messages: ConversationMessage[] = detail?.messages ?? [];
  const hasExistingApp = !!html && !isGenerating;
  const isDemoProject = !!detail?.is_demo;

  // 当前展示版本的大小（KB），与 SHA-256 一起构成一致性标识（S5.2）
  const htmlSizeKb = html ? (html.length / 1024).toFixed(1) : null;
  const versionMeta =
    activeSeq != null && html ? (
      <span className="shrink-0 rounded-md border border-slate-800 bg-slate-900/60 px-2 py-0.5 font-mono text-[10px] text-slate-400">
        v{activeSeq} · {htmlSizeKb}KB · SHA {htmlSha256 ? `${htmlSha256.slice(0, 8)}…` : '—'}
      </span>
    ) : null;

  return (
    <div className="flex h-screen flex-col bg-slate-950 text-slate-100">
      {/* ---------------- 顶栏 ---------------- */}
      <header className="flex shrink-0 items-center gap-3 border-b border-slate-800/80 bg-slate-900/50 px-5 py-3 backdrop-blur">
        <div className="flex h-8 w-8 items-center justify-center rounded-xl bg-gradient-to-br from-sky-500 to-violet-600">
          <Wand2 className="h-4 w-4 text-white" />
        </div>
        <div>
          <h1 className="text-sm font-semibold tracking-wide">Atoms Studio</h1>
          <p className="text-[10px] text-slate-500">智能体驱动的应用生成平台</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {isGenerating && (
            <span className="flex items-center gap-1.5 rounded-full bg-sky-500/10 px-3 py-1 text-[11px] text-sky-300">
              <Loader2 className="h-3 w-3 animate-spin" />
              智能体工作中
            </span>
          )}
          {/* 认证入口（S2）：匿名可用，登录是显式按钮，绝不自动跳转 */}
          {authStatus === 'loading' ? (
            <span className="flex items-center gap-1.5 px-2 text-[11px] text-slate-500">
              <Loader2 className="h-3 w-3 animate-spin" />
              登录态检查中
            </span>
          ) : authStatus === 'authenticated' && user ? (
            <>
              <span className="hidden max-w-40 truncate text-[11px] text-slate-400 sm:block">
                {user.name || user.email || user.id}
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => void handleLogout()}
                className="gap-1.5 rounded-xl border-slate-700 bg-transparent text-slate-300 hover:bg-slate-800 hover:text-white"
              >
                <LogOut className="h-3.5 w-3.5" />
                退出
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="outline"
              onClick={login}
              className="gap-1.5 rounded-xl border-slate-700 bg-transparent text-slate-300 hover:bg-sky-500/10 hover:text-sky-300"
            >
              <LogIn className="h-3.5 w-3.5" />
              登录（可选）
            </Button>
          )}
          <Button
            size="sm"
            variant="outline"
            onClick={handleNewProject}
            className="gap-1.5 rounded-xl border-slate-700 bg-transparent text-slate-300 hover:bg-slate-800 hover:text-white"
          >
            <Plus className="h-3.5 w-3.5" />
            新项目
          </Button>
        </div>
      </header>

      {/* ---------------- 三栏主体 ---------------- */}
      <div className="flex min-h-0 flex-1">
        {/* 左栏：项目列表 */}
        <aside className="flex w-64 shrink-0 flex-col border-r border-slate-800/80 bg-slate-900/30">
          <div className="flex items-center gap-2 px-4 py-3">
            <History className="h-3.5 w-3.5 text-slate-500" />
            <span className="text-xs font-medium text-slate-400">我的项目</span>
            <span className="ml-auto text-[10px] text-slate-600">{projects.length}</span>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
            <ProjectList
              projects={projects}
              loading={projectsLoading}
              activeId={activeId}
              onCreate={handleNewProject}
              onSelect={(id) => void openProject(id)}
              onDelete={(id) => void handleDelete(id)}
            />
          </div>
        </aside>

        {/* 中栏：智能体步骤 + 对话 + 输入 */}
        <section className="flex w-[380px] shrink-0 flex-col border-r border-slate-800/80">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-4">
            {/* 智能体工作流 */}
            <div>
              <div className="mb-2 flex items-center gap-2">
                <Bot className="h-3.5 w-3.5 text-sky-400" />
                <span className="text-xs font-medium text-slate-300">智能体工作流</span>
                {isGenerating && (
                  <span className="text-[10px] text-slate-500">三阶段流水线执行中</span>
                )}
              </div>
              {steps.length ? (
                <AgentSteps steps={steps} failedMessage={failedMessage} />
              ) : (
                <div className="rounded-xl border border-dashed border-slate-800 px-4 py-6 text-center">
                  <Sparkles className="mx-auto h-5 w-5 text-slate-600" />
                  <p className="mt-2 text-xs text-slate-500">
                    提交描述后，这里会实时展示
                    <br />
                    需求分析 → 结构设计 → 代码生成
                  </p>
                </div>
              )}
            </div>

            {/* 对话记录 */}
            {messages.length > 0 && (
              <div>
                <div className="mb-2 flex items-center gap-2">
                  <MessageSquare className="h-3.5 w-3.5 text-slate-500" />
                  <span className="text-xs font-medium text-slate-300">对话记录</span>
                </div>
                <div className="rounded-xl border border-slate-800 bg-slate-900/40 p-3">
                  <ConversationPanel messages={messages} />
                </div>
              </div>
            )}
          </div>

          {/* 输入区 */}
          <div className="shrink-0 border-t border-slate-800/80 p-3">
            {failedMessage && !isGenerating && (
              <div className="mb-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] leading-relaxed text-rose-300">
                <div className="flex items-start gap-2">
                  <span className="min-w-0 flex-1">
                    {failedMessage}
                    {/* 分类标签：让「超出时间预算」（平台侧止损）与
                        「上游响应超时」（上游不响应）在界面上即可区分 */}
                    {failedType && (
                      <span className="ml-1.5 rounded border border-rose-500/30 px-1 py-0.5 font-mono text-[10px]">
                        {ERROR_TYPE_LABELS[failedType] || failedType}
                      </span>
                    )}
                  </span>
                  <button
                    type="button"
                    onClick={handleRetry}
                    disabled={!submittedPrompt || isDemoProject}
                    className="shrink-0 rounded border border-rose-400/40 bg-rose-500/20 px-2 py-0.5 font-medium text-rose-100 transition-colors hover:bg-rose-500/30 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    用原描述重新生成
                  </button>
                </div>
                <div className="mt-1 text-rose-400/90">
                  {ERROR_TYPE_HINTS[failedType || ''] ||
                    '你的描述已保留，可直接重新生成'}
                </div>
              </div>
            )}
            <PromptInput
              value={prompt}
              onChange={setPrompt}
              onSubmit={() => void handleSubmit()}
              onStop={() => void handleStop()}
              isGenerating={isGenerating}
              hasProject={!!activeId && hasExistingApp}
              disabled={isDemoProject}
            />
          </div>
        </section>

        {/* 右栏：预览 / 源码 */}
        <main className="flex min-w-0 flex-1 flex-col">
          <div className="flex shrink-0 items-center gap-2 border-b border-slate-800/80 px-4 py-2">
            <div className="flex rounded-lg border border-slate-800 bg-slate-900/60 p-0.5">
              <button
                type="button"
                onClick={() => setView('preview')}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1 text-xs transition-colors ${
                  view === 'preview'
                    ? 'bg-sky-500/20 text-sky-200'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <MonitorPlay className="h-3.5 w-3.5" />
                应用预览
              </button>
              <button
                type="button"
                onClick={() => setView('code')}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1 text-xs transition-colors ${
                  view === 'code'
                    ? 'bg-sky-500/20 text-sky-200'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <Code2 className="h-3.5 w-3.5" />
                源代码
              </button>
            </div>
            <div className="ml-2 min-w-0 flex-1">
              <VersionSwitcher
                versions={versions}
                activeSeq={activeSeq}
                onSelect={(seq) => void switchVersion(seq)}
                onRestore={(seq) => void handleRestore(seq)}
              />
            </div>
            {versionMeta}
            {detail && (
              <span className="shrink-0 truncate text-xs text-slate-500">{detail.title}</span>
            )}
          </div>

          {/* 预览失败原因条（S5.4）：展示失败版本的可读原因与上游错误分类 */}
          {failedMessage && !isGenerating && (
            <div className="flex shrink-0 items-center gap-2 border-b border-rose-500/20 bg-rose-500/10 px-4 py-1.5 text-[11px] text-rose-300">
              <span className="font-medium">预览不可用：</span>
              <span className="min-w-0 flex-1 truncate">{failedMessage}</span>
              {failedType && (
                <span className="shrink-0 rounded border border-rose-500/30 px-1.5 py-0.5 font-mono text-[10px]">
                  {ERROR_TYPE_LABELS[failedType] || failedType}
                </span>
              )}
            </div>
          )}

          <div className="min-h-0 flex-1">
            {detailLoading ? (
              <div className="flex h-full items-center justify-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                加载项目内容…
              </div>
            ) : view === 'preview' ? (
              <PreviewPane
                html={html}
                isGenerating={isGenerating}
                emptyHint={
                  activeId
                    ? '该项目还没有成功生成的版本，在左侧输入要求开始生成'
                    : '在左侧描述你想要的应用，智能体会为你生成可交互的网页应用'
                }
              />
            ) : (
              <CodeViewer html={html} seq={activeSeq} sha256={htmlSha256} />
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
