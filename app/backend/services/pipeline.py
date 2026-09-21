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
- 每次模型调用用 ``asyncio.wait_for`` 施加 ``min(STAGE_TIMEOUT, remaining)``
  显式超时（T004），整体受 ``GENERATION_BUDGET_SECONDS`` 单调预算约束，
  预算耗尽时立即以 ``budget_exhausted`` 止损（T005）。
- 心跳已删除（T006）：``versions.updated_at`` 只在真实进展点推进
  （``_finish_step`` 写摘要、``_fail`` 写终态、成功收尾写终态），
  活任务最长静默期 = 整体预算 420s < ``STALE_AFTER`` 600s，长任务不被误杀。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from time import monotonic

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

# 时间预算（002-harden-increment-session T003）：
# - STAGE_TIMEOUT=120s：单次模型调用超时，取在上游实测 126.2s 返回 524 的
#   切断点之内，避免触碰网关代理超时；
# - GENERATION_BUDGET_SECONDS=420s：单次生成整体上限（入口单调截止）；
# - 不等关系 120 < 420 < 600(STALE_AFTER) < 720(前端轮询上限 12min)
#   由 tests/test_stale_recovery.py::test_timing_constants_ordering 守护。
STAGE_TIMEOUT = 120.0
GENERATION_BUDGET_SECONDS = 420.0
# 预算耗尽止损（T005）：remaining 低于该值时不再发起新的模型调用。
MIN_CALL_BUDGET_SECONDS = 5.0

# S3.1 退避重试：rate_limit / timeout / upstream_5xx / unknown 最多 2 次尝试，
# 间隔 0.5 → 1 秒。auth 不重试。收紧次数使最坏路径落在整体预算内。
RETRY_BACKOFF_SECONDS = (0.5, 1.0)
MAX_ATTEMPTS = 2

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
    # T005：整体预算耗尽止损文案
    "budget_exhausted": "生成耗时超过整体时间预算，已提前终止，请稍后重试",
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
    2. :meth:`run` —— 执行三阶段 AI 调用，其间每个阶段用独立短 DB 阶段更新状态；
       整体受 ``GENERATION_BUDGET_SECONDS`` 单调预算约束（心跳已删除，T006）。

    ``ai`` 参数供测试注入 FakeAIHub（S4）；生产默认 ``AIHubService()``。
    """

    def __init__(self, db: AsyncSession, ai: Any | None = None) -> None:
        self._db = db
        self._ai_override = ai
        self._ai_service: Any | None = None
        # T004：流水线整体的单调截止时刻，run() 入口设置；
        # 直接构造而未 run 时为 None，视为无预算约束（便于单元测试）。
        self._deadline: float | None = None
        # T031：流水线实际所在阶段，由 _call_step 进入时记录，供通用兜底归因。
        self._current_step = 0

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

        # T004：入口计算单调截止时刻，整条流水线受 GENERATION_BUDGET_SECONDS 约束。
        self._deadline = monotonic() + GENERATION_BUDGET_SECONDS
        return await self._run_stages(
            started,
            project_public_id,
            version_seq,
            prompt,
            previous_html,
            history_prompts,
        )

    def _remaining(self) -> float:
        """T004：距整体预算截止的剩余秒数；无截止（单测直构）时视为无限。"""
        if self._deadline is None:
            return float("inf")
        return self._deadline - monotonic()

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
            # T031：归因到流水线实际所在阶段（_call_step 进入时记录），不硬编码 3；
            # summary 记录异常类名保留可观测性。不把该异常移进 _call_step 的重试
            # try——数据库/代码故障不是可重试的上游错误（research.md R-4）。
            failed_seq = self._current_step or 3
            message = "模型服务暂时不可用，请稍后重试"
            await self._fail(
                project_public_id,
                version_seq,
                failed_seq,
                message,
                error_type="unknown",
                attempts=1,
                exception_class=type(exc).__name__,
            )
            return {
                "status": "failed",
                "message": message,
                "failed_seq": failed_seq,
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

        S3.1 与 002 特性的恢复策略：
        - 每次调用用 ``asyncio.wait_for`` 施加 ``min(STAGE_TIMEOUT, remaining)``
          显式超时（T004）；
        - auth 类错误不重试，立即失败 + ERROR 日志；
        - rate_limit / timeout / upstream_5xx / unknown 退避重试（0.5→1s，最多 2 次）；
        - 空内容先原样重试一次，再降 max_tokens 试一次；
        - 整体预算耗尽时不再发起新的模型调用，以 ``budget_exhausted`` 止损（T005）。

        阶段边界取消检查：用户在上一阶段执行期间点了「停止生成」时，
        版本已被取消接口置为 cancelled，这里不再发起新的模型调用。
        """
        # T031：记录流水线实际所在阶段，供 run() 的通用兜底归因使用。
        self._current_step = step_seq
        version = await self._get_version(project_public_id, version_seq)
        if version and version.status == "cancelled":
            raise GenerationCancelled(step_seq)

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
            attempts_used = attempt + 1
            # T005：预算耗尽止损——剩余时间不足以支撑一次有意义的调用时，
            # 立即以 budget_exhausted 失败，不再进入重试循环。
            if self._remaining() < MIN_CALL_BUDGET_SECONDS:
                raise PipelineError(
                    UPSTREAM_USER_MESSAGES["budget_exhausted"],
                    step_seq,
                    error_type="budget_exhausted",
                    attempts=attempts_used - 1,
                )
            call_started = _now()
            try:
                response = await asyncio.wait_for(
                    self._ai.gentxt(request),
                    # T004：单次调用超时不超过整体预算剩余时间
                    timeout=min(STAGE_TIMEOUT, self._remaining()),
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
        exception_class: str | None = None,
    ) -> None:
        # T032：清扫残留——把该版本所有仍处于 running/pending 的步骤一并置为
        # failed（数据库/代码故障不会给步骤留下自然终态），归因步骤写失败原因。
        steps_result = await self._db.execute(
            select(Generation_steps)
            .where(
                Generation_steps.project_public_id == project_public_id,
                Generation_steps.version_seq == version_seq,
            )
            .execution_options(populate_existing=True)
        )
        for step in steps_result.scalars().all():
            if step.seq == step_seq:
                step.status = "failed"
                step.output = message
                step.ended_at = step.ended_at or _iso(_now())
            elif step.status in ACTIVE_STATUSES:
                step.status = "failed"
                step.output = "已随失败收尾终止"
                step.ended_at = step.ended_at or _iso(_now())
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
            if exception_class:
                existing["exception_class"] = exception_class
            if (
                error_type
                or upstream_status is not None
                or attempts is not None
                or exception_class
            ):
                version.summary = json.dumps(existing, ensure_ascii=False)
        project = await self._get_project(project_public_id)
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "failed"
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
