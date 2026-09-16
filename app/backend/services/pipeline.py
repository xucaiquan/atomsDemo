"""三阶段智能体流水线编排。

对应 specs/001-atoms-demo/research.md R1（三阶段串行）与 R10（先落库后执行）。

流水线契约：
- 提交时即创建 ``versions`` 记录（status=pending）并预置 3 条 ``generation_steps``
  （status=pending），使前端提交后立即拿到完整步骤列表。
- 各阶段开始时把步骤置 ``running`` 并写 ``started_at``，结束时置 ``succeeded``
  并写 ``ended_at`` 与该阶段原始产出 ``output``（为回放提供基础）。
- 任一阶段失败：该步骤置 ``failed``，版本置 ``failed`` 并写入**面向用户的可读中文**原因。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.generation_steps import Generation_steps
from models.messages import Messages
from models.projects import Projects
from models.versions import Versions
from schemas.aihub import ChatMessage, GenTxtRequest
from services.aihub import AIHubService
from services.html_extract import extract_html, inject_csp, is_complete_document
from services import prompts

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

ACTIVE_STATUSES = ("pending", "running")
FALLBACK_TITLE = "未命名项目"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
    """携带面向用户可读中文措辞的流水线错误。"""

    def __init__(self, message: str, step_seq: int) -> None:
        super().__init__(message)
        self.message = message
        self.step_seq = step_seq


class GenerationPipeline:
    """三阶段生成流水线。

    使用方式（严格遵循数据库会话边界规则：慢的 AI 调用前后各自是独立的短 DB 阶段）：

    1. :meth:`prepare` —— 短 DB 阶段：校验并发、落库版本与 3 条步骤，然后 commit。
    2. :meth:`run` —— 执行三阶段 AI 调用，其间每个阶段用独立短 DB 阶段更新状态。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db
        self._ai = AIHubService()

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
    ) -> dict[str, Any]:
        """执行三阶段流水线并把结果落库。

        Returns:
            成功：``{"status": "succeeded", "html": ..., "duration_ms": ..., "title": ..., "steps": [...]}``
            失败：``{"status": "failed", "message": ..., "failed_seq": ..., "steps": [...]}``
        """
        started = _now()
        await self._set_version_status(project_public_id, version_seq, "running")

        try:
            # ---------- 阶段 1 需求分析 ----------
            analysis_raw = await self._call_step(
                project_public_id,
                version_seq,
                step_seq=1,
                model=FAST_MODEL,
                system=prompts.ANALYZE_SYSTEM,
                user=prompts.build_analyze_user(prompt, previous_html),
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
                user=prompts.build_design_user(prompt, analysis_raw, previous_html),
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
            )

            code_summary = f"产出可运行页面，约 {len(html) // 1024 or 1} KB"
            await self._finish_step(project_public_id, version_seq, 3, html, code_summary)

        except PipelineError as exc:
            await self._fail(project_public_id, version_seq, exc.step_seq, exc.message)
            return {
                "status": "failed",
                "message": exc.message,
                "failed_seq": exc.step_seq,
                "steps": await self.load_steps(project_public_id, version_seq),
            }
        except Exception as exc:  # noqa: BLE001 - 兜底为可读中文提示
            logger.exception("生成流水线异常: %s", exc)
            message = "模型服务暂时不可用，请稍后重试"
            await self._fail(project_public_id, version_seq, 3, message)
            return {
                "status": "failed",
                "message": message,
                "failed_seq": 3,
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

        return {
            "status": "succeeded",
            "html": html,
            "duration_ms": duration_ms,
            "title": project.title if project else title,
            "summary": summary,
            "steps": await self.load_steps(project_public_id, version_seq),
        }

    # ------------------------------------------------------------------ 内部工具

    async def _generate_code(
        self,
        project_public_id: str,
        version_seq: int,
        prompt: str,
        analysis_raw: str,
        design_raw: str,
        previous_html: str | None,
    ) -> str:
        """阶段 3 代码生成：截断续写 + 整篇重跑兜底。

        内容密集型需求（如「每日菜谱推荐」需要大量菜品数据）极易被
        max_tokens 截断。恢复策略优先「续写」：把已产出内容的末尾片段
        回传给模型，让它从中断处接着写，拼接后校验，最多两轮；
        仍不完整则整篇重跑一次（模型可能产出更紧凑的完整页面）；
        最终仍失败才抛出可读错误。
        """
        user = prompts.build_code_user(prompt, analysis_raw, design_raw, previous_html)
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
                if is_complete_document(doc):
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
                        ChatMessage(role="assistant", content=doc),
                    ],
                )
                cont = extract_html(cont_raw)
                if not cont:
                    break
                if _DOC_RESTART_PATTERN.match(cont):
                    doc = cont  # 模型重开了整篇文档，直接采用新产出
                else:
                    doc = _merge_continuation(doc, cont)
            if is_complete_document(doc):
                return inject_csp(doc)
            logger.warning("代码生成第 %s 轮续写后仍不完整", attempt + 1)
        raise PipelineError(
            "生成的页面内容不完整（可能被截断），请简化需求后重试", 3
        )

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

        空内容重试一次：推理模型偶发把 token 预算耗在思考链上导致
        content 为空（research.md 风险 2），重试是最低成本的恢复路径。
        """
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
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = await self._ai.gentxt(request)
            except Exception as exc:  # noqa: BLE001
                logger.exception("阶段 %s 模型调用失败: %s", step_seq, exc)
                last_error = exc
                continue

            content = (getattr(response, "content", "") or "").strip()
            if content:
                return content
            logger.warning("阶段 %s 第 %s 次调用返回空内容", step_seq, attempt + 1)

        if last_error is not None:
            raise PipelineError("模型服务暂时不可用，请稍后重试", step_seq) from last_error
        raise PipelineError("模型返回了空内容，请重新提交生成", step_seq)

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

    async def _fail(
        self,
        project_public_id: str,
        version_seq: int,
        step_seq: int,
        message: str,
    ) -> None:
        step = await self._get_step(project_public_id, version_seq, step_seq)
        if step:
            step.status = "failed"
            step.output = message
            step.ended_at = _iso(_now())
        version = await self._get_version(project_public_id, version_seq)
        if version:
            version.status = "failed"
            version.error = message
        project = await self._get_project(project_public_id)
        if project:
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

    async def _get_project(self, public_id: str) -> Projects | None:
        result = await self._db.execute(
            select(Projects).where(Projects.public_id == public_id)
        )
        return result.scalars().first()

    async def _get_version(self, public_id: str, seq: int) -> Versions | None:
        result = await self._db.execute(
            select(Versions).where(
                Versions.project_public_id == public_id, Versions.seq == seq
            )
        )
        return result.scalars().first()

    async def _get_step(
        self, public_id: str, version_seq: int, step_seq: int
    ) -> Generation_steps | None:
        result = await self._db.execute(
            select(Generation_steps).where(
                Generation_steps.project_public_id == public_id,
                Generation_steps.version_seq == version_seq,
                Generation_steps.seq == step_seq,
            )
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
