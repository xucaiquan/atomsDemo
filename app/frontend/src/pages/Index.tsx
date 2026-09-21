/**
 * Workspace —— 主工作区（三栏布局）。
 *
 * 核心流程（对应 specs/001-atoms-demo）：
 * 1. 提交瞬间本地构造 3 条步骤骨架并渲染，不等任何网络往返（SC-002「3 秒内首个可见反馈」）
 * 2. 生成期间轮询步骤状态，把真实进程映射到步骤流转（US4 / FR-008）
 * 3. 成功后渲染进沙箱 iframe；失败时保留用户输入并给出可读中文提示（FR-011）
 * 4. 项目列表、版本切换、对话记录、源码查看全部持久化可找回（US2 / US3）
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  Bot,
  Code2,
  History,
  Loader2,
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

export default function Index() {
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
  const [html, setHtml] = useState('');
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

  const openProject = useCallback(async (publicId: string) => {
    setActiveId(publicId);
    setDetailLoading(true);
    setFailedMessage(null);
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
        } catch {
          setHtml('');
        }
        setSteps(latest.steps);
      } else {
        setActiveSeq(null);
        setHtml('');
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

  // ---------------- 版本切换（US3 / FR-007） ----------------
  const switchVersion = useCallback(
    async (seq: number) => {
      if (!activeId) return;
      setActiveSeq(seq);
      setView('preview');
      try {
        const full = await atomsApi.getVersion(activeId, seq);
        setHtml(full.html || '');
        if (full.status === 'failed') setFailedMessage(full.error);
        else setFailedMessage(null);
        const version = detail?.versions.find((v) => v.seq === seq);
        if (version) setSteps(version.steps);
      } catch (e) {
        toast.error((e as Error).message || '版本内容加载失败');
      }
    },
    [activeId, detail],
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
        setActiveSeq(full.seq);
        setFailedMessage(null);
        toast.success('生成完成，在右侧直接体验');
      } catch {
        setFailedMessage('生成结果加载失败，可切换版本重试');
      }
    } else if (snapshot.status === 'cancelled') {
      // 用户主动停止：不算失败，展示取消态步骤，旧版本保持可用
      setFailedMessage(null);
      if (snapshot.steps.length) setSteps(snapshot.steps);
      toast.info('已停止本次生成，之前的版本不受影响');
    } else {
      setFailedMessage(snapshot.error || '生成失败，你的描述已保留');
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
  const handleSubmit = useCallback(async () => {
    const text = prompt.trim();
    if (!text || isGenerating) return;
    setFailedMessage(null);
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
      //    旧实现同步等待三阶段（3 次模型调用，1~4 分钟），超过网关 120s
      //    代理读超时被掐断 —— 这正是贪吃蛇生成失败的根因。
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
      // 受理前的校验错误（400/404/409）：输入保留（FR-011），骨架回退失败态
      setFailedMessage(error.message || '生成失败，请稍后重试');
      toast.error(error.message || '生成失败，你的描述已保留');
      setSteps((prev) =>
        prev.map((s, i) =>
          s.status === 'running' || i === 0
            ? { ...s, status: 'failed', output: error.message }
            : s,
        ),
      );
    }
  }, [prompt, isGenerating, activeId, awaitGeneration, finishGeneration, stopPolling]);

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
  // 调用后端取消接口：版本与活跃步骤立即落库为 cancelled，进程内后台任务被
  // task.cancel() 即时中断；进行中的轮询检测到 cancelled 终态后自然收尾。
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
    setSteps([]);
    setActiveSeq(null);
    setFailedMessage(null);
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
                {failedMessage}（你的描述已保留，可直接重试）
              </div>
            )}
            <PromptInput
              value={prompt}
              onChange={setPrompt}
              onSubmit={() => void handleSubmit()}
              onStop={() => void handleStop()}
              isGenerating={isGenerating}
              hasProject={!!activeId && hasExistingApp}
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
              />
            </div>
            {detail && (
              <span className="shrink-0 truncate text-xs text-slate-500">{detail.title}</span>
            )}
          </div>

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
              <CodeViewer html={html} />
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
