"""三阶段智能体流水线编排。

对应 specs/001-atoms-demo/research.md R1（三阶段串行）与 R10（先落库后执行），
以及设计文档 2026-09-20 S3（生成链路稳定性）：

- 提交时即创建 ``versions`` 记录（status=pending）并预置 3 条 ``generation_steps``
  （status=pending），使前端提交后立即拿到完整步骤列表。
- 各阶段开始时把步骤置 ``running`` 并写 ``started_at``，结束时置 ``succeeded``
  并写 ``ended_at`` 与该阶段原始产出 ``output``（为回放提供基础）。
- 任一阶段失败：该步骤置 ``failed``，版本置 ``failed`` 并写入**面向用户的可读中文**原因，
  同时在 ``versions.summary`` 里持久化 ``error_type`` / ``upstream_status`` / ``attempts``
  便于排障（S3.5）。
- 上游故障按 kind 分类处理：auth 不重试；rate_limit/timeout/upstream_5xx/unknown
  退避重试；空内容降级 max_tokens 再试；截断走续写/重跑链（S3.1）。
- 每次模型调用用 ``asyncio.wait_for`` 施加 ``min(STAGE_TIMEOUT, 剩余预算)`` 显式
  超时，且整条流水线共享一个 ``GENERATION_BUDGET_SECONDS`` 总预算（S3.2）。
  预算而非心跳才是「长任务不被误杀」的保证：活任务最迟在预算内落终态，
  而预算 < routers 侧的 STALE_AFTER，故不等式恒成立。曾经的心跳任务已删除，
  原因见本文件末尾的注释。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.generation_steps import Generation_steps
from models.messages import Messages
from models.projects import Projects
from models.versions import Versions
from schemas.aihub import ChatMessage, GenTxtRequest
from services import prompts
from services.aihub import AIHubService
from services.aihub_errors import UpstreamError
from services.html_extract import (
    extract_html,
    has_duplicate_structure,
    inject_csp,
    is_complete_document,
    looks_well_formed,
)

logger = logging.getLogger(__name__)

# 阶段模型选型：前两阶段输出短、要求快；代码生成阶段追求质量并避免 HTML 截断。
FAST_MODEL = "deepseek-v4-flash"
CODE_MODEL = "deepseek-v4-pro"

# 代码生成阶段的输出预算。推理模型的思考链会占用输出，放宽以降低截断概率。
CODE_MAX_TOKENS = 16384
# 续写时回传给模型的中断位置上下文长度（字符）。
CONTINUE_TAIL_CHARS = 2000
# 续写产出若以完整文档开头，视为模型重开了整篇文档，直接采用新产出。
_DOC_RESTART_PATTERN = re.compile(r"<!DOCTYPE\s+html|<html[\s>]", re.IGNORECASE)

# 时间预算。四者必须严格有序（不变量见
# uploads/specs/002-harden-increment-session/contracts/generation-lifecycle.md）：
#
#   STAGE_TIMEOUT(120s) < GENERATION_BUDGET_SECONDS(420s)
#                       < STALE_AFTER(600s) < 前端轮询上限(720s)
#
# - 120 < 420：单次调用必须能在总预算内跑完一次并留有恢复余地，否则预算被
#   单次调用吃光，任何恢复都不可能。
# - 420 < 600：活任务的最长静默期（阶段 3 的预算）必须短于回收阈值，否则活
#   任务会被误杀——这正是心跳当初要解决的问题，现在由预算本身解决。
# - 600 < 720：回收必须先于前端放弃，否则用户永远等不到真实终态。
#
# STAGE_TIMEOUT 另受上游行为约束：实测上游网关在 126.2s 处返回 524，
# 单次等待设在其内侧，避免为一个已被上游丢弃的请求白等 114s。
STAGE_TIMEOUT = 120.0
# 整条流水线（阶段 1+2+3 及其全部重试、续写、重跑）从起点到终态的总预算。
# 修复前的漏洞：6 次 _call_step × 每次最坏 3×240s+退避 = 4329s ≈ 72 分钟，
# 而前端只等 12 分钟。预算必须**整体**生效而非逐次生效。
GENERATION_BUDGET_SECONDS = 420.0

# S3.1 退避重试：rate_limit / timeout / upstream_5xx / unknown 最多 2 次尝试，
# 间隔 0.5 → 1 秒。auth 不重试。次数由 3 收紧到 2，因为 3 次重试的乘积
# 已超出总预算，重试的意义只有在预算内才有价值。
RETRY_BACKOFF_SECONDS = (0.5, 1.0)
MAX_ATTEMPTS = 2

# 预算耗尽：阶段发起新调用前，若剩余预算低于此值则认为「跑不完一次」，
# 不再发起调用，直接以可读失败落库（而不是把预算耗在一次注定超时的调用上）。
MIN_CALL_BUDGET_SECONDS = 5.0
# 预算耗尽的错误类型，与上游超时（timeout）区分：前者是平台侧主动止损，
# 后者是上游不响应。
BUDGET_EXHAUSTED_ERROR_TYPE = "budget_exhausted"

ACTIVE_STATUSES = ("pending", "running")
FALLBACK_TITLE = "未命名项目"

# kind -> 面向用户的可读中文（重试耗尽后的最终文案）
UPSTREAM_USER_MESSAGES = {
    "auth": "模型服务鉴权失败，请联系管理员",
    "rate_limit": "模型服务繁忙（限流），请稍后重试",
    "timeout": "模型服务响应超时，请稍后重试",
    "upstream_5xx": "模型服务暂时不可用，请稍后重试",
    "empty": "模型返回了空内容，请重新提交生成",
    "truncated": "生成的页面内容不完整（可能被截断），请简化需求后重试",
    "unknown": "模型服务暂时不可用，请稍后重试",
    # 平台侧主动止损（非上游故障）：与 timeout 文案刻意区分，让用户知道
    # 「不是模型坏了，是这次生成超过了时间预算」。
    BUDGET_EXHAUSTED_ERROR_TYPE: "本次生成超出时间预算，描述已保留，可重新提交",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def classify_upstream_error(exc: Exception) -> UpstreamError:
    """把底层异常映射为可分类的 UpstreamError（S3.1）。

    openai SDK 的异常类型按名称匹配（避免对 openai 版本的硬依赖）；
    映射失败时归 unknown 且 retriable=True——宁可多试一次。
    """
    name = type(exc).__name__
    status_code: int | None = getattr(exc, "status_code", None)
    if status_code is None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)

    if name in ("AuthenticationError", "PermissionDeniedError") or status_code in (401, 403):
        return UpstreamError("auth", str(exc), status_code, retriable=False)
    if name == "RateLimitError" or status_code == 429:
        return UpstreamError("rate_limit", str(exc), status_code)
    if name in ("APITimeoutError", "APIConnectionError", "TimeoutError", "ConnectionError") or isinstance(
        exc, asyncio.TimeoutError
    ):
        return UpstreamError("timeout", str(exc), status_code)
    if name == "InternalServerError" or (status_code is not None and status_code >= 500):
        return UpstreamError("upstream_5xx", str(exc), status_code)
    return UpstreamError("unknown", str(exc), status_code)


def _extract_json_block(text: str) -> str:
    """从模型输出中抽取 JSON 块，容忍 Markdown 围栏与前后缀文字。"""
    body = text.strip()
    if body.startswith("```"):
        match = re.search(r"```(?:json)?\s*\n(.*?)```", body, re.DOTALL)
        if match:
            body = match.group(1).strip()
    start = body.find("{")
    end = body.rfind("}")
    if start >= 0 and end > start:
        return body[start:end + 1]
    return body


def _parse_json_payload(text: str) -> dict[str, Any] | None:
    """尽力把模型输出解析成 dict；失败返回 None（调用方降级为纯文本使用）。"""
    try:
        payload = json.loads(_extract_json_block(text))
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _summarize_analysis(payload: dict[str, Any] | None, raw: str) -> tuple[str, str]:
    """返回 (应用名, 步骤展示摘要)。"""
    if not payload:
        return "", "已完成需求理解"
    app_name = str(payload.get("app_name") or "").strip()[:60]
    features = payload.get("features")
    if isinstance(features, list) and features:
        summary = f"识别出 {len(features)} 项核心功能：" + "、".join(
            str(item).strip() for item in features[:3]
        )
    else:
        summary = (payload.get("notes") or raw[:60] or "已完成需求理解").strip()
    return app_name, summary[:180]


def _summarize_design(payload: dict[str, Any] | None, raw: str) -> str:
    if not payload:
        return "已完成结构设计"
    components = payload.get("components")
    if isinstance(components, list) and components:
        return f"规划出 {len(components)} 个界面区域：" + "、".join(
            str(item).strip()[:24] for item in components[:3]
        )
    layout = str(payload.get("layout") or raw[:60] or "已完成结构设计").strip()
    return layout[:180]


def _fallback_title(prompt: str) -> str:
    """标题缺失时回退为用户描述的前 20 字（对应 data-model.md 的回退逻辑）。"""
    cleaned = " ".join(prompt.split())
    return (cleaned[:20] or FALLBACK_TITLE)[:120]


def _merge_continuation(existing: str, continuation: str) -> str:
    """把续写片段拼接到已产出文档末尾，去掉模型重复输出的重叠部分。

    模型续写时偶尔会复述中断点前的少量文字，直接拼接会产生重复片段。
    这里在 existing 末尾 300 字符窗口内寻找与 continuation 开头的最大重叠。
    """
    continuation = continuation.strip()
    if not continuation:
        return existing
    tail = existing[-min(len(existing), 300):]
    for size in range(min(len(tail), len(continuation)), 0, -1):
        if tail.endswith(continuation[:size]):
            return existing + continuation[size:]
    return f"{existing}\n{continuation}"


class PipelineError(Exception):
    """携带面向用户可读中文措辞的流水线错误。

    ``error_type`` / ``upstream_status`` / ``attempts`` 为 S3.5 可观测性字段，
    会被持久化进 ``versions.summary``；保留 (message, step_seq) 位置参数兼容。
    """

    def __init__(
        self,
        message: str,
        step_seq: int,
        retriable: bool = False,
        error_type: str | None = None,
        upstream_status: int | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step_seq = step_seq
        self.retriable = retriable
        self.error_type = error_type
        self.upstream_status = upstream_status
        self.attempts = attempts


class GenerationCancelled(Exception):
    """用户在阶段边界取消生成（版本已被取消接口置为 cancelled）。"""

    def __init__(self, step_seq: int) -> None:
        super().__init__("生成已取消")
        self.step_seq = step_seq


class GenerationPipeline:
    """三阶段生成流水线。

    使用方式（严格遵循数据库会话边界规则：慢的 AI 调用前后各自是独立的短 DB 阶段）：

    1. :meth:`prepare` —— 短 DB 阶段：校验并发、落库版本与 3 条步骤，然后 commit。
    2. :meth:`run` —— 执行三阶段 AI 调用，其间每个阶段用独立短 DB 阶段更新状态。
       整条流水线受 ``GENERATION_BUDGET_SECONDS`` 总预算约束，超时即主动止损
       并落可读终态（不再有心跳任务——见文件末尾注释）。

    ``ai`` 参数供测试注入 FakeAIHub（S4）；生产默认 ``AIHubService()``。
    """

    def __init__(self, db: AsyncSession, ai: Any | None = None) -> None:
        self._db = db
        self._ai_override = ai
        self._ai_service: Any | None = None
        # 总预算的绝对截止点（time.monotonic 秒）。None 表示预算未启动——
        # prepare()/load_steps() 等纯落库方法不设预算，_remaining() 返回 inf。
        # 实例化点见 routers/atoms.py（每次生成新建一个 pipeline），故实例状态
        # 不会跨生成串味。
        self._deadline: float | None = None
        # 当前正在执行的 step_seq。通用兜底异常处理器靠它把故障归因到真正
        # 失败的阶段，而不是硬编码 3（见 T031）。
        self._current_step_seq: int | None = None
        # 当前阶段已消耗的上游调用次数。通用兜底同样要把它写进失败记录：
        # 「本地代码故障」时这个值是 0，正好把「一次上游调用都没发生」这件事
        # 记录下来，与「上游重试耗尽」区分开。
        self._current_attempts: int = 0

    def _remaining(self) -> float:
        """距总预算耗尽还剩多少秒（未启动预算时为无穷大）。"""
        if self._deadline is None:
            return float("inf")
        return self._deadline - time.monotonic()

    @property
    def _ai(self) -> Any:
        """惰性构造 AIHubService：prepare（纯落库阶段）不触碰 AI；

        测试注入 FakeAIHub 时完全不实例化真实客户端（S4 可测性改造）。
        """
        if self._ai_override is not None:
            return self._ai_override
        if self._ai_service is None:
            self._ai_service = AIHubService()
        return self._ai_service

    # ------------------------------------------------------------------ 落库阶段

    async def prepare(self, project: Projects, prompt: str) -> dict[str, Any]:
        """先落库后执行：创建 pending 版本 + 预置 3 条 pending 步骤。

        Returns:
            包含 ``version_seq`` 与 ``steps`` 的字典，供 API 立即回给前端。
        """
        existing = await self._db.execute(
            select(Versions).where(Versions.project_public_id == project.public_id)
        )
        versions = list(existing.scalars().all())
        next_seq = max((v.seq for v in versions), default=0) + 1

        version = Versions(
            project_public_id=project.public_id,
            seq=next_seq,
            prompt=prompt,
            status="pending",
        )
        self._db.add(version)

        for idx, name in enumerate(prompts.STEP_NAMES, start=1):
            self._db.add(
                Generation_steps(
                    project_public_id=project.public_id,
                    version_seq=next_seq,
                    seq=idx,
                    name=name,
                    status="pending",
                )
            )

        # 用户消息在生成开始前即持久化，保证失败时描述不丢失（FR-011）。
        self._db.add(
            Messages(
                project_public_id=project.public_id,
                role="user",
                content=prompt,
            )
        )

        project.latest_status = "pending"
        project.version_count = next_seq
        await self._db.commit()

        return {
            "version_seq": next_seq,
            "steps": [
                {"seq": idx, "name": name, "status": "pending"}
                for idx, name in enumerate(prompts.STEP_NAMES, start=1)
            ],
        }

    # ------------------------------------------------------------------ 执行阶段

    async def run(
        self,
        project_public_id: str,
        version_seq: int,
        prompt: str,
        previous_html: str | None,
        history_prompts: list[str] | None = None,
    ) -> dict[str, Any]:
        """执行三阶段流水线并把结果落库。

        ``history_prompts`` 是本项目此前**成功版本**的需求列表（时间升序），
        用于让模型消解「继续刚刚的需求」「按之前说的」这类指代。

        Returns:
            成功：``{"status": "succeeded", "html": ..., "duration_ms": ..., "title": ..., "steps": [...]}``
            失败：``{"status": "failed", "message": ..., "failed_seq": ..., "steps": [...]}``
        """
        started = _now()
        # 受理与启动之间用户可能已点「停止生成」：版本被置为 cancelled 时不再启动。
        current = await self._get_version(project_public_id, version_seq)
        if current and current.status == "cancelled":
            await self._finalize_cancelled(project_public_id, version_seq)
            return {
                "status": "cancelled",
                "message": "生成已取消",
                "steps": await self.load_steps(project_public_id, version_seq),
            }
        await self._set_version_status(project_public_id, version_seq, "running")

        # 预算从真正开始跑的阶段起算（受理到启动之间的排队不算在用户的等待预算里）。
        # 整条流水线的所有调用共享这一个截止点，杜绝「每次调用各自 240s」的乘积放大。
        self._deadline = time.monotonic() + GENERATION_BUDGET_SECONDS

        return await self._run_stages(
            started,
            project_public_id,
            version_seq,
            prompt,
            previous_html,
            history_prompts,
        )

    async def _run_stages(
        self,
        started: datetime,
        project_public_id: str,
        version_seq: int,
        prompt: str,
        previous_html: str | None,
        history_prompts: list[str] | None,
    ) -> dict[str, Any]:
        try:
            # ---------- 阶段 1 需求分析 ----------
            analysis_raw = await self._call_step(
                project_public_id,
                version_seq,
                step_seq=1,
                model=FAST_MODEL,
                system=prompts.ANALYZE_SYSTEM,
                user=prompts.build_analyze_user(prompt, previous_html, history_prompts),
            )
            analysis_payload = _parse_json_payload(analysis_raw)
            app_name, analysis_summary = _summarize_analysis(analysis_payload, analysis_raw)
            await self._finish_step(
                project_public_id, version_seq, 1, analysis_raw, analysis_summary
            )

            # ---------- 阶段 2 结构设计 ----------
            design_raw = await self._call_step(
                project_public_id,
                version_seq,
                step_seq=2,
                model=FAST_MODEL,
                system=prompts.DESIGN_SYSTEM,
                user=prompts.build_design_user(
                    prompt, analysis_raw, previous_html, history_prompts
                ),
            )
            design_payload = _parse_json_payload(design_raw)
            design_summary = _summarize_design(design_payload, design_raw)
            await self._finish_step(
                project_public_id, version_seq, 2, design_raw, design_summary
            )

            # ---------- 阶段 3 代码生成 ----------
            # 完整自包含 HTML 体积大且推理模型思考链占用输出预算，内容密集型
            # 需求极易截断，交由 _generate_code 做「截断续写 + 整篇重跑」兜底。
            html = await self._generate_code(
                project_public_id,
                version_seq,
                prompt,
                analysis_raw,
                design_raw,
                previous_html,
                history_prompts,
            )

            code_summary = f"产出可运行页面，约 {len(html) // 1024 or 1} KB"
            await self._finish_step(project_public_id, version_seq, 3, html, code_summary)

        except GenerationCancelled:
            await self._finalize_cancelled(project_public_id, version_seq)
            return {
                "status": "cancelled",
                "message": "生成已取消",
                "steps": await self.load_steps(project_public_id, version_seq),
            }
        except PipelineError as exc:
            await self._fail(
                project_public_id,
                version_seq,
                exc.step_seq,
                exc.message,
                error_type=exc.error_type,
                upstream_status=exc.upstream_status,
                attempts=exc.attempts,
            )
            return {
                "status": "failed",
                "message": exc.message,
                "failed_seq": exc.step_seq,
                "error_type": exc.error_type,
                "steps": await self.load_steps(project_public_id, version_seq),
            }
        except Exception as exc:  # noqa: BLE001 - 兜底为可读中文提示
            logger.exception("生成流水线异常: %s", exc)
            message = "模型服务暂时不可用，请稍后重试"
            # 归因到**实际所在阶段**（SC-005）。硬编码 3 有两个后果：阶段 1/2 的
            # 本地故障被写成「阶段 3 失败」，以及真正出错的步骤永远停在 running
            # （_fail 只会把被归因的那一步置为 failed）——违反 FR-014/FR-015。
            # 尚无阶段进入时（异常发生在第一次 _call_step 之前）归到阶段 1，那是
            # 流水线本来要去的地方；此时 attempts 为 0，如实记录「一次上游调用都
            # 没发生」。
            step_seq = self._current_step_seq or 1
            await self._fail(
                project_public_id,
                version_seq,
                step_seq,
                message,
                error_type="unknown",
                attempts=self._current_attempts,
                exception=type(exc).__name__,
            )
            return {
                "status": "failed",
                "message": message,
                "failed_seq": step_seq,
                "error_type": "unknown",
                "steps": await self.load_steps(project_public_id, version_seq),
            }

        duration_ms = int((_now() - started).total_seconds() * 1000)
        summary = {
            "features": (analysis_payload or {}).get("features") or [],
            "structure": design_summary,
            "app_name": app_name,
        }
        title = app_name or _fallback_title(prompt)

        version = await self._get_version(project_public_id, version_seq)
        if version and version.status not in ACTIVE_STATUSES:
            # 收尾前用户已取消：丢弃结果，不覆盖取消状态
            return {
                "status": "cancelled",
                "message": version.error or "生成已取消",
                "steps": await self.load_steps(project_public_id, version_seq),
            }
        if version:
            version.status = "succeeded"
            version.html = html
            version.summary = json.dumps(summary, ensure_ascii=False)
            version.duration_ms = duration_ms
            version.error = None

        project = await self._get_project(project_public_id)
        if project:
            project.latest_status = "succeeded"
            # 首次生成时用阶段 1 产出的应用名覆盖占位标题。
            if version_seq == 1 or project.title in ("", FALLBACK_TITLE, None):
                project.title = title

        self._db.add(
            Messages(
                project_public_id=project_public_id,
                role="assistant",
                content=f"已生成「{title}」·{analysis_summary}",
                version_seq=version_seq,
            )
        )
        await self._db.commit()

        logger.info(
            "project=%s seq=%s step=done model=%s attempt=- duration_ms=%s",
            project_public_id[:8],
            version_seq,
            CODE_MODEL,
            duration_ms,
        )
        return {
            "status": "succeeded",
            "html": html,
            "duration_ms": duration_ms,
            "title": project.title if project else title,
            "summary": summary,
            "steps": await self.load_steps(project_public_id, version_seq),
        }

    # ------------------------------------------------------- 关于已删除的心跳
    #
    # 这里曾有一个 _heartbeat：每 30s 用独立 DB 会话无条件刷 versions.updated_at，
    # 目的是让长任务不被 _recover_stale_versions 误杀。它已被删除，且**不得以任何
    # 形式复活**——因为它恰恰是回收机制失效的原因：
    #
    #   回收判据 = running 且 updated_at 超过 STALE_AFTER
    #   心跳行为 = 活着的任务每 30s 刷新 updated_at
    #   ⇒ 活着的任务永远不满足回收判据；而死掉的任务因为不再有心跳，
    #     仍然要等满 STALE_AFTER。心跳没有区分「活着」与「死了」，
    #     它只是把回收阈值变成了摆设。
    #
    # 正确的解法是让「活任务的最长静默期」本身短于回收阈值。先看清
    # versions.updated_at 到底由谁推进：
    #
    #   _call_step 置步骤 running 时改的是 **generation_steps** 行，不推进
    #   versions.updated_at；真正推进它的是 _finish_step（写阶段摘要）
    #   与 _run_stages 的成功落库 / _fail 写终态。
    #
    # 因此活任务在 versions 上的最长静默期 = 两个阶段边界之间的最长时间
    #   ≤ 整条流水线预算 GENERATION_BUDGET_SECONDS = 420s
    #   <  STALE_AFTER = 600s
    #
    # 不等式由预算本身保证，不需要任何后台刷新任务。
    #
    # 不要为了「防止误杀」把它加回来。要么维持 420 < 600，要么改回收判据，
    # 两者只能选一个。

    # ------------------------------------------------------------------ 阶段 3 恢复链

    async def _generate_code(
        self,
        project_public_id: str,
        version_seq: int,
        prompt: str,
        analysis_raw: str,
        design_raw: str,
        previous_html: str | None,
        history_prompts: list[str] | None = None,
    ) -> str:
        """阶段 3 代码生成：截断续写 + 整篇重跑兜底。

        内容密集型需求（如「每日菜谱推荐」需要大量菜品数据）极易被
        max_tokens 截断。恢复策略优先「续写」：把已产出内容的末尾片段
        回传给模型，让它从中断处接着写，拼接后校验，最多两轮；
        仍不完整则整篇重跑一次（模型可能产出更紧凑的完整页面）；
        最终仍失败才抛出可读错误。

        判定升级为 ``is_complete_document and looks_well_formed``（S3.4）：
        拼接后若出现重复文档结构（模型续写时重开了整篇文档），直接采用
        新产出而不是把两份文档拼在一起。
        """
        user = prompts.build_code_user(
            prompt, analysis_raw, design_raw, previous_html, history_prompts
        )
        for attempt in range(2):
            code_raw = await self._call_step(
                project_public_id,
                version_seq,
                step_seq=3,
                model=CODE_MODEL,
                system=prompts.CODE_SYSTEM,
                user=user,
                max_tokens=CODE_MAX_TOKENS,
            )
            doc = extract_html(code_raw)
            for round_no in range(2):
                lowered = doc.lower()
                if "<html" not in lowered and "<body" not in lowered:
                    break  # 不是有效页面，直接进入整篇重跑
                if is_complete_document(doc) and looks_well_formed(doc):
                    return inject_csp(doc)
                logger.warning(
                    "代码生成第 %s 轮产出截断（长度 %s），尝试续写第 %s 次",
                    attempt + 1,
                    len(doc),
                    round_no + 1,
                )
                cont_raw = await self._call_step(
                    project_public_id,
                    version_seq,
                    step_seq=3,
                    model=CODE_MODEL,
                    system=prompts.CODE_SYSTEM,
                    user=prompts.build_code_continue_user(doc[-CONTINUE_TAIL_CHARS:]),
                    max_tokens=CODE_MAX_TOKENS,
                    history=[
                        ChatMessage(role="user", content=user),
                        # 上下文预算闸门（S3.3）：只回传已产出文档的尾部片段
                        ChatMessage(
                            role="assistant",
                            content=prompts.truncate_continue_history(doc),
                        ),
                    ],
                )
                cont = extract_html(cont_raw)
                if not cont:
                    break
                if _DOC_RESTART_PATTERN.match(cont):
                    doc = cont  # 模型重开了整篇文档，直接采用新产出
                else:
                    doc = _merge_continuation(doc, cont)
                    if has_duplicate_structure(doc):
                        # 正则 match 只匹配开头，模型在开头多输出一句解释就会
                        # 漏判重开；用结构计数兜底，采用更完整的新产出。
                        logger.warning("续写拼接后检测到重复文档结构，改用新产出")
                        doc = cont
                if is_complete_document(doc) and looks_well_formed(doc):
                    return inject_csp(doc)
            if is_complete_document(doc) and looks_well_formed(doc):
                return inject_csp(doc)
            logger.warning("代码生成第 %s 轮续写后仍不完整", attempt + 1)
        raise PipelineError(UPSTREAM_USER_MESSAGES["truncated"], 3, error_type="truncated")

    # ------------------------------------------------------------------ 模型调用

    async def _call_step(
        self,
        project_public_id: str,
        version_seq: int,
        step_seq: int,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        history: list[ChatMessage] | None = None,
    ) -> str:
        """把步骤置 running 后调用模型（非流式，便于完整校验产出）。

        S3.1/S3.2 的恢复策略：
        - 每次调用用 ``asyncio.wait_for`` 施加 ``min(STAGE_TIMEOUT, 剩余预算)``
          显式超时，单次调用不可能越过总截止点；
        - auth 类错误不重试，立即失败 + ERROR 日志；
        - rate_limit / timeout / upstream_5xx / unknown 退避重试（0.5→1s，最多 2 次）；
        - 空内容先原样重试一次，再降 max_tokens 试一次。

        阶段边界取消检查：用户在上一阶段执行期间点了「停止生成」时，
        版本已被取消接口置为 cancelled，这里不再发起新的模型调用。

        预算闸门：剩余预算低于 MIN_CALL_BUDGET_SECONDS 时不再发起调用，
        直接抛 PipelineError(budget_exhausted)。闸门放在置 running **之前**——
        一个因预算不足而根本没跑的阶段不该在库里留下「已启动」的假象。
        """
        # 记录「流水线当前在这个阶段」。必须在任何可能抛错的动作之前记录——
        # 下面 _get_step / commit / GenTxtRequest 构造都在重试 try 之外（R-4），
        # 它们抛出的异常由 _run_stages 的通用兜底接管，兜底只能靠这里的状态
        # 才能把故障归因到真正的阶段（T031）。
        self._current_step_seq = step_seq
        self._current_attempts = 0

        version = await self._get_version(project_public_id, version_seq)
        if version and version.status == "cancelled":
            raise GenerationCancelled(step_seq)

        remaining = self._remaining()
        if remaining < MIN_CALL_BUDGET_SECONDS:
            logger.error(
                "project=%s seq=%s step=%s 预算耗尽，放弃发起调用 "
                "remaining=%.1fs budget=%.0fs",
                project_public_id[:8],
                version_seq,
                step_seq,
                remaining,
                GENERATION_BUDGET_SECONDS,
            )
            raise PipelineError(
                UPSTREAM_USER_MESSAGES[BUDGET_EXHAUSTED_ERROR_TYPE],
                step_seq,
                error_type=BUDGET_EXHAUSTED_ERROR_TYPE,
                # 如实记录 0 次：这一步一次上游调用都没发起。省略该字段会让
                # 「根本没试」与「试过但失败了」在失败记录里无法区分（FR-013）。
                attempts=self._current_attempts,
            )

        step = await self._get_step(project_public_id, version_seq, step_seq)
        if step:
            step.status = "running"
            step.started_at = _iso(_now())
        await self._db.commit()  # 关闭 DB 阶段，避免事务跨越慢的 AI 调用

        request = GenTxtRequest(
            messages=[
                ChatMessage(role="system", content=system),
                *(history or []),
                ChatMessage(role="user", content=user),
            ],
            model=model,
            max_tokens=max_tokens,
        )

        attempts_used = 0
        last_upstream: UpstreamError | None = None
        for attempt in range(MAX_ATTEMPTS):
            # 重试前重算预算：上一次尝试可能已把剩余预算吃到不足以再跑一次。
            # 第 0 次不检查——进入循环前已过闸门。
            if attempt > 0 and self._remaining() < MIN_CALL_BUDGET_SECONDS:
                logger.error(
                    "project=%s seq=%s step=%s 预算不足，放弃第 %s 次尝试 "
                    "remaining=%.1fs",
                    project_public_id[:8],
                    version_seq,
                    step_seq,
                    attempt + 1,
                    self._remaining(),
                )
                break
            attempts_used = attempt + 1
            self._current_attempts = attempts_used  # 供通用兜底补齐 attempts
            call_started = _now()
            # 单次等待取「阶段上限」与「剩余预算」的较小者；下限保底
            # MIN_CALL_BUDGET_SECONDS，避免剩余预算归零时 wait_for(0) 立刻
            # 空转超时、把预算耗尽误报成上游 timeout。
            timeout = min(
                STAGE_TIMEOUT, max(self._remaining(), MIN_CALL_BUDGET_SECONDS)
            )
            try:
                response = await asyncio.wait_for(
                    self._ai.gentxt(request), timeout=timeout
                )
            except Exception as exc:  # noqa: BLE001
                upstream = (
                    exc
                    if isinstance(exc, UpstreamError)
                    else classify_upstream_error(exc)
                )
                duration_ms = int((_now() - call_started).total_seconds() * 1000)
                logger.error(
                    "project=%s seq=%s step=%s model=%s attempt=%s duration_ms=%s "
                    "upstream_kind=%s status=%s err=%s",
                    project_public_id[:8],
                    version_seq,
                    step_seq,
                    model,
                    attempts_used,
                    duration_ms,
                    upstream.kind,
                    upstream.status_code,
                    upstream,
                )
                last_upstream = upstream
                if not upstream.retriable:
                    break
                if attempt < MAX_ATTEMPTS - 1:
                    await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
                continue

            duration_ms = int((_now() - call_started).total_seconds() * 1000)
            logger.info(
                "project=%s seq=%s step=%s model=%s attempt=%s duration_ms=%s",
                project_public_id[:8],
                version_seq,
                step_seq,
                model,
                attempts_used,
                duration_ms,
            )
            content = (getattr(response, "content", "") or "").strip()
            if content:
                return content

            # 空内容：最后一次尝试降 max_tokens（推理模型偶发把预算耗在思考链上）
            logger.warning(
                "阶段 %s 第 %s 次调用返回空内容", step_seq, attempts_used
            )
            last_upstream = UpstreamError("empty", "模型返回空内容", None)
            if attempt == 0 and request.max_tokens and request.max_tokens > 4096:
                request = request.model_copy(
                    update={"max_tokens": max(4096, request.max_tokens // 2)}
                )
            if attempt < MAX_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])

        kind = last_upstream.kind if last_upstream else "unknown"
        message = UPSTREAM_USER_MESSAGES.get(kind, UPSTREAM_USER_MESSAGES["unknown"])
        raise PipelineError(
            message,
            step_seq,
            error_type=kind,
            upstream_status=last_upstream.status_code if last_upstream else None,
            attempts=attempts_used,
        ) from last_upstream

    # ------------------------------------------------------------------ 内部工具

    async def _finish_step(
        self,
        project_public_id: str,
        version_seq: int,
        step_seq: int,
        output: str,
        summary: str,
    ) -> None:
        step = await self._get_step(project_public_id, version_seq, step_seq)
        if step:
            step.status = "succeeded"
            step.output = output
            step.ended_at = _iso(_now())
        version = await self._get_version(project_public_id, version_seq)
        if version:
            existing = _parse_json_payload(version.summary or "") or {}
            existing[f"step{step_seq}"] = summary
            version.summary = json.dumps(existing, ensure_ascii=False)
        await self._db.commit()

    async def _finalize_cancelled(
        self, project_public_id: str, version_seq: int
    ) -> None:
        """阶段边界检测到取消：把仍活跃的收尾步骤与项目状态落为 cancelled。"""
        steps_result = await self._db.execute(
            select(Generation_steps)
            .where(
                Generation_steps.project_public_id == project_public_id,
                Generation_steps.version_seq == version_seq,
            )
            .execution_options(populate_existing=True)
        )
        for step in steps_result.scalars().all():
            if step.status in ACTIVE_STATUSES:
                step.status = "cancelled"
                step.output = step.output or "已取消"
                step.ended_at = step.ended_at or _iso(_now())
        version = await self._get_version(project_public_id, version_seq)
        if version and version.status in ACTIVE_STATUSES:
            version.status = "cancelled"
            version.error = "生成已取消"
        project = await self._get_project(project_public_id)
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "cancelled"
        await self._db.commit()

    async def _fail(
        self,
        project_public_id: str,
        version_seq: int,
        step_seq: int,
        message: str,
        error_type: str | None = None,
        upstream_status: int | None = None,
        attempts: int | None = None,
        exception: str | None = None,
    ) -> None:
        step = await self._get_step(project_public_id, version_seq, step_seq)
        if step:
            step.status = "failed"
            step.output = message
            step.ended_at = _iso(_now())
        # 收尾清扫（FR-015 / INV-S2）：把该版本下**仍为 running** 的步骤一并置为
        # failed。只清 running——已成功的阶段必须保持 succeeded（它们是有效的
        # 中间产物），尚未开始的 pending 也不该被改写。
        # 没有这一步时，任何归因偏差都会留下永久 running 的步骤，前端与轮询接口
        # 都会一直显示「生成中」。
        stale = await self._db.execute(
            select(Generation_steps)
            .where(
                Generation_steps.project_public_id == project_public_id,
                Generation_steps.version_seq == version_seq,
                Generation_steps.status == "running",
            )
            .execution_options(populate_existing=True)
        )
        for running_step in stale.scalars().all():
            running_step.status = "failed"
            running_step.output = message
            running_step.ended_at = _iso(_now())
        version = await self._get_version(project_public_id, version_seq)
        # 取消守卫：版本已被取消接口置为 cancelled 时，迟到的失败不得覆盖取消态
        if version and version.status in ACTIVE_STATUSES:
            version.status = "failed"
            version.error = message
            # S3.5 失败可观测：错误分类持久化进 summary JSON（不改模型列）
            existing = _parse_json_payload(version.summary or "") or {}
            if error_type:
                existing["error_type"] = error_type
            if upstream_status is not None:
                existing["upstream_status"] = upstream_status
            if attempts is not None:
                existing["attempts"] = attempts
            # 异常类名：本地代码故障没有上游状态码可归因，类名是唯一能区分
            # 「我们自己的 bug」与「上游不响应」的可观测线索。
            if exception:
                existing["exception"] = exception
            if (
                error_type
                or upstream_status is not None
                or attempts is not None
                or exception
            ):
                version.summary = json.dumps(existing, ensure_ascii=False)
        project = await self._get_project(project_public_id)
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "failed"
        # 会话记录同样受取消守卫约束：用户已点「停止生成」，再追加一句「生成失败」
        # 会把「我停的」说成「它坏了」，并引导用户去重试一个他刚放弃的请求。
        # 只排除取消态，其它情况（含版本行缺失）保持原有行为不变。
        if not (version and version.status == "cancelled"):
            self._db.add(
                Messages(
                    project_public_id=project_public_id,
                    role="assistant",
                    content=f"生成失败：{message}",
                    version_seq=version_seq,
                )
            )
        await self._db.commit()

    async def _set_version_status(
        self, project_public_id: str, version_seq: int, status: str
    ) -> None:
        version = await self._get_version(project_public_id, version_seq)
        if version:
            version.status = status
        project = await self._get_project(project_public_id)
        if project:
            project.latest_status = status
        await self._db.commit()

    # 取消接口在**另一个 DB 会话**写库，而本会话的流水线对象在 commit 后
    # 不会过期（expire_on_commit=False）。默认的身份映射行为是「已加载对象
    # 不被数据库结果覆盖」，于是阶段边界取消检查与失败守卫会读到陈旧状态、
    # 守卫形同虚设。所有跨会话状态读取一律 populate_existing 强制刷新。
    async def _get_project(self, public_id: str) -> Projects | None:
        result = await self._db.execute(
            select(Projects)
            .where(Projects.public_id == public_id)
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def _get_version(self, public_id: str, seq: int) -> Versions | None:
        result = await self._db.execute(
            select(Versions)
            .where(
                Versions.project_public_id == public_id, Versions.seq == seq
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def _get_step(
        self, public_id: str, version_seq: int, step_seq: int
    ) -> Generation_steps | None:
        result = await self._db.execute(
            select(Generation_steps)
            .where(
                Generation_steps.project_public_id == public_id,
                Generation_steps.version_seq == version_seq,
                Generation_steps.seq == step_seq,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def load_steps(
        self, public_id: str, version_seq: int
    ) -> list[dict[str, Any]]:
        result = await self._db.execute(
            select(Generation_steps)
            .where(
                Generation_steps.project_public_id == public_id,
                Generation_steps.version_seq == version_seq,
            )
            .order_by(Generation_steps.seq)
            .execution_options(populate_existing=True)
        )
        steps = list(result.scalars().all())
        return [
            {
                "seq": step.seq,
                "name": step.name,
                "status": step.status,
                "output": (step.output or "")[:2000],
                "started_at": step.started_at,
                "ended_at": step.ended_at,
            }
            for step in steps
        ]
