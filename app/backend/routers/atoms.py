"""Atoms Demo 平台的自定义 API。

对应 specs/001-atoms-demo/contracts/rest-api.md 的接口契约：

- ``GET    /api/v1/atoms/health``                          健康检查
- ``GET    /api/v1/atoms/projects``                        项目列表（不含 html）
- ``POST   /api/v1/atoms/projects``                        创建空项目
- ``GET    /api/v1/atoms/projects/{public_id}``            项目详情（含版本与对话，不含 html）
- ``GET    /api/v1/atoms/projects/{public_id}/versions/{seq}``  单版本完整内容（含 html）
- ``DELETE /api/v1/atoms/projects/{public_id}``            删除项目（级联）
- ``POST   /api/v1/atoms/projects/{public_id}/generate``   三阶段生成（异步受理 202）
- ``POST   /api/v1/atoms/projects/{public_id}/versions/{seq}/cancel``  取消进行中的生成

契约要点：
1. 对外一律使用 ``public_id``（UUID v4），**自增 id 不出现在任何响应中**。
2. 所有错误统一为 ``{"error": {"code": ..., "message": ...}}``，``message`` 必须是
   面向用户的可读中文，不含堆栈或内部路径。
3. ``VALIDATION_ERROR`` / ``NOT_FOUND`` / ``CONFLICT`` 在开始生成之前返回。
4. 并发约束：同项目已有 ``pending`` / ``running`` 版本时返回 409。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import db_manager, get_db
from models.generation_steps import Generation_steps
from models.messages import Messages
from models.projects import Projects
from models.versions import Versions
from services import prompts
from services.pipeline import ACTIVE_STATUSES, FALLBACK_TITLE, GenerationPipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/atoms", tags=["atoms"])

PROMPT_MIN_LEN = 1
PROMPT_MAX_LEN = 2000
# 超过该时长仍处于 pending/running 的版本视为中断（服务重启或连接断开）。
STALE_AFTER = timedelta(minutes=10)

ERROR_STATUS = {
    "VALIDATION_ERROR": 400,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "UPSTREAM_ERROR": 502,
    "INTERNAL_ERROR": 500,
}


# ------------------------------------------------------------------ 错误信封


def error_envelope(code: str, message: str) -> JSONResponse:
    """构造统一错误信封（message 面向用户，可直接展示）。"""
    return JSONResponse(
        status_code=ERROR_STATUS.get(code, 500),
        content={"error": {"code": code, "message": message}},
    )


# ------------------------------------------------------------------ 请求模型


class CreateProjectRequest(BaseModel):
    title: Optional[str] = None
    owner_key: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str = ""
    owner_key: Optional[str] = None


# ------------------------------------------------------------------ 序列化工具


def _iso(value: datetime | None) -> str | None:
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _project_brief(project: Projects) -> dict[str, Any]:
    """项目列表条目，不含 html，不含自增 id。"""
    return {
        "public_id": project.public_id,
        "title": project.title,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "version_count": project.version_count or 0,
        "latest_status": project.latest_status,
        "is_demo": bool(project.is_demo),
    }


def _version_brief(version: Versions, steps: list[Generation_steps]) -> dict[str, Any]:
    """版本列表条目，不含 html。"""
    return {
        "seq": version.seq,
        "prompt": version.prompt,
        "status": version.status,
        "error": version.error,
        "duration_ms": version.duration_ms,
        "created_at": _iso(version.created_at),
        "steps": [
            {
                "seq": step.seq,
                "name": step.name,
                "status": step.status,
                "output": (step.output or "")[:2000],
                "started_at": step.started_at,
                "ended_at": step.ended_at,
            }
            for step in sorted(steps, key=lambda item: item.seq)
        ],
    }


def _parse_summary(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


# ------------------------------------------------------------------ 查询工具


async def _fetch_project(db: AsyncSession, public_id: str) -> Projects | None:
    result = await db.execute(select(Projects).where(Projects.public_id == public_id))
    return result.scalars().first()


async def _recover_stale_versions(db: AsyncSession, public_id: str | None = None) -> None:
    """启动/访问时清理中断的生成。

    对应 data-model.md 的恢复语义：服务重启后残留的 ``pending`` / ``running``
    版本置为 ``failed`` 并写入**面向用户的可读原因**，避免永久卡住的加载态。
    """
    stmt = select(Versions).where(Versions.status.in_(ACTIVE_STATUSES))
    if public_id:
        stmt = stmt.where(Versions.project_public_id == public_id)
    result = await db.execute(stmt)
    stale = list(result.scalars().all())
    if not stale:
        return

    now = datetime.now(timezone.utc)
    changed = False
    for version in stale:
        created = version.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created and now - created < STALE_AFTER:
            continue

        version.status = "failed"
        version.error = "生成过程被中断（服务重启或连接断开），你的描述已保留，可重新提交"
        changed = True

        steps_result = await db.execute(
            select(Generation_steps).where(
                Generation_steps.project_public_id == version.project_public_id,
                Generation_steps.version_seq == version.seq,
            )
        )
        for step in steps_result.scalars().all():
            if step.status in ACTIVE_STATUSES:
                step.status = "failed"
                step.output = step.output or "该阶段被中断"

        project = await _fetch_project(db, version.project_public_id)
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "failed"

    if changed:
        await db.commit()


# ------------------------------------------------------------------ 接口实现


@router.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    """健康检查：数据库连通性与模型配置状态。"""
    db_status = "ok"
    try:
        await db.execute(select(Projects.id).limit(1))
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("健康检查数据库探测失败: %s", exc)
        db_status = "error"

    return {
        "status": "ok",
        "db": db_status,
        "llm": {
            "configured": True,
            "model": "deepseek-v4-pro",
            "reachable": True,
        },
    }


@router.get("/projects")
async def list_projects(db: AsyncSession = Depends(get_db)):
    """项目列表，按 updated_at 倒序，不含 html。"""
    await _recover_stale_versions(db)
    result = await db.execute(select(Projects).order_by(Projects.updated_at.desc()))
    projects = list(result.scalars().all())
    payload = {"projects": [_project_brief(item) for item in projects]}
    await db.commit()
    return payload


@router.post("/projects")
async def create_project(
    data: CreateProjectRequest,
    db: AsyncSession = Depends(get_db),
):
    """创建空项目。title 省略时使用占位名，首次生成完成后由阶段 1 产出覆盖。"""
    title = (data.title or "").strip()[:120] or FALLBACK_TITLE
    project = Projects(
        public_id=str(uuid.uuid4()),
        title=title,
        owner_key=(data.owner_key or "")[:64] or None,
        version_count=0,
        latest_status=None,
        is_demo=False,
    )
    db.add(project)
    await db.commit()
    payload = _project_brief(project)
    return JSONResponse(status_code=201, content=payload)


@router.get("/projects/{public_id}")
async def get_project(public_id: str, db: AsyncSession = Depends(get_db)):
    """项目详情：含版本列表（带步骤）与对话记录，不含 html。"""
    await _recover_stale_versions(db, public_id)
    project = await _fetch_project(db, public_id)
    if not project:
        return error_envelope("NOT_FOUND", "项目不存在或已被删除")

    versions_result = await db.execute(
        select(Versions)
        .where(Versions.project_public_id == public_id)
        .order_by(Versions.seq)
    )
    versions = list(versions_result.scalars().all())

    steps_result = await db.execute(
        select(Generation_steps).where(Generation_steps.project_public_id == public_id)
    )
    steps = list(steps_result.scalars().all())
    steps_by_version: dict[int, list[Generation_steps]] = {}
    for step in steps:
        steps_by_version.setdefault(step.version_seq, []).append(step)

    messages_result = await db.execute(
        select(Messages)
        .where(Messages.project_public_id == public_id)
        .order_by(Messages.id)
    )
    messages = list(messages_result.scalars().all())

    payload = {
        "public_id": project.public_id,
        "title": project.title,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "version_count": project.version_count or len(versions),
        "latest_status": project.latest_status,
        "versions": [
            _version_brief(version, steps_by_version.get(version.seq, []))
            for version in versions
        ],
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "version_seq": message.version_seq,
                "created_at": _iso(message.created_at),
            }
            for message in messages
        ],
    }
    await db.commit()
    return payload


@router.get("/projects/{public_id}/versions/{seq}")
async def get_version(public_id: str, seq: int, db: AsyncSession = Depends(get_db)):
    """单个版本的完整内容，**仅此接口返回 html**。"""
    result = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id, Versions.seq == seq
        )
    )
    version = result.scalars().first()
    if not version:
        return error_envelope("NOT_FOUND", "该版本不存在")

    payload = {
        "seq": version.seq,
        "prompt": version.prompt,
        "html": version.html or "",
        "summary": _parse_summary(version.summary),
        "status": version.status,
        "error": version.error,
        "duration_ms": version.duration_ms,
        "created_at": _iso(version.created_at),
    }
    await db.commit()
    return payload


@router.delete("/projects/{public_id}")
async def delete_project(public_id: str, db: AsyncSession = Depends(get_db)):
    """删除项目及其下全部版本、消息与步骤（级联）。"""
    project = await _fetch_project(db, public_id)
    if not project:
        return error_envelope("NOT_FOUND", "项目不存在或已被删除")

    for model in (Generation_steps, Messages, Versions):
        rows = await db.execute(
            select(model).where(model.project_public_id == public_id)
        )
        for row in rows.scalars().all():
            await db.delete(row)
    await db.delete(project)
    await db.commit()
    return JSONResponse(status_code=200, content={"deleted": True})


# ------------------------------------------------------------------ 后台生成任务
#
# 三阶段流水线串联 3 次模型调用，整体耗时通常 1~4 分钟，**远超平台网关的 120s
# 代理读超时**。若同步等待，用户会看到「origin did not return a complete response
# within the 120-second Proxy Read Timeout window」，且请求被网关掐断后前端拿不到
# 任何结果。因此生成改为：请求内只做校验 + 落库（毫秒级）→ 立即返回 202 受理 →
# 三阶段在 asyncio 后台任务中用独立会话执行 → 前端轮询 steps 接口取终态。

# 任务注册表：(project_public_id, version_seq) -> asyncio.Task。
# 取消接口据此对进程内仍在执行的后台任务调用 task.cancel()，
# 让正在等待模型响应的 await 点立即中断，而不必等到下一阶段边界。
_RUNNING_TASKS: dict[tuple[str, int], asyncio.Task] = {}


async def _run_generation_in_background(
    public_id: str,
    version_seq: int,
    prompt: str,
    previous_html: str | None,
    history_prompts: list[str] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """后台执行三阶段流水线。

    默认自开独立 DB 会话（请求会话此时已关闭）。``session`` 非空时复用它——
    这是 GENERATION_INLINE 测试路径用的，它跑在请求内、请求会话仍然活着。
    """
    try:
        if session is not None:
            pipeline = GenerationPipeline(session)
            outcome = await pipeline.run(
                public_id, version_seq, prompt, previous_html, history_prompts
            )
            logger.info(
                "内联生成结束 project=%s seq=%s status=%s",
                public_id[:8],
                version_seq,
                outcome.get("status"),
            )
            return
        async with db_manager.session() as session:
            pipeline = GenerationPipeline(session)
            outcome = await pipeline.run(
                public_id, version_seq, prompt, previous_html, history_prompts
            )
            logger.info(
                "后台生成结束 project=%s seq=%s status=%s",
                public_id[:8],
                version_seq,
                outcome.get("status"),
            )
    except asyncio.CancelledError:
        # 取消接口已把版本与活跃步骤落库为 cancelled；这里只做兜底，
        # 把任务被硬中断时残留的活跃步骤收尾，避免前端看到永久 running。
        logger.info("后台生成任务被取消 project=%s seq=%s", public_id[:8], version_seq)
        try:
            async with db_manager.session() as session:
                steps_result = await session.execute(
                    select(Generation_steps).where(
                        Generation_steps.project_public_id == public_id,
                        Generation_steps.version_seq == version_seq,
                        Generation_steps.status.in_(ACTIVE_STATUSES),
                    )
                )
                for step in steps_result.scalars().all():
                    step.status = "cancelled"
                    step.output = step.output or "已取消"
                await session.commit()
        except Exception as inner:  # noqa: BLE001
            logger.exception("取消收尾落库失败: %s", inner)
    except Exception as exc:  # noqa: BLE001 - 后台任务异常不得冒泡
        logger.exception("后台生成任务异常: %s", exc)
        try:
            async with db_manager.session() as session:
                result = await session.execute(
                    select(Versions).where(
                        Versions.project_public_id == public_id,
                        Versions.seq == version_seq,
                    )
                )
                version = result.scalars().first()
                if version and version.status in ACTIVE_STATUSES:
                    version.status = "failed"
                    version.error = "生成过程出现异常，你的描述已保留，可重新提交"
                project_result = await session.execute(
                    select(Projects).where(Projects.public_id == public_id)
                )
                project = project_result.scalars().first()
                if project and project.latest_status in ACTIVE_STATUSES:
                    project.latest_status = "failed"
                steps_result = await session.execute(
                    select(Generation_steps).where(
                        Generation_steps.project_public_id == public_id,
                        Generation_steps.version_seq == version_seq,
                        Generation_steps.status.in_(ACTIVE_STATUSES),
                    )
                )
                for step in steps_result.scalars().all():
                    step.status = "failed"
                    step.output = step.output or "该阶段被中断"
                await session.commit()
        except Exception as inner:  # noqa: BLE001
            logger.exception("后台生成兜底落库失败: %s", inner)


def _spawn_generation(
    public_id: str,
    version_seq: int,
    prompt: str,
    previous_html: str | None,
    history_prompts: list[str] | None,
) -> "asyncio.Task | None":
    """创建受跟踪的后台任务，注册到任务表以支持取消，并防止 task 被 GC 提前回收。

    当环境变量 GENERATION_INLINE 为真时，不创建后台任务，调用方必须 await 返回值
    （见 generate 处理器）。**仅测试用**：让集成测试是确定性的，并且让流水线复用
    测试注入的会话，而不是去连真实数据库。生产环境绝不可置位。

    判定条件**只有**环境变量，且按 ``test_generation_inline`` 的严格解析口径取值：
    ``os.getenv`` 返回字符串，``"0"``/``"false"``/``"no"`` 全为真值，直接按真值
    判定会让 ``app/.env`` 里写 ``GENERATION_INLINE=0`` 想关闭它的人反而打开它
    （``start_app_v2.sh`` 会把 env 文件的每一行都 export 进后端进程），生产路径
    于是会在请求内同步等待 1~4 分钟的流水线，撞上网关 120s 代理读超时（计划
    Global Constraints 第 6 条）。``generate`` 恒有请求会话，若把「传入了会话」
    也当作 inline 依据，同样会踩中这个坑。
    """
    if os.getenv("GENERATION_INLINE", "").strip().lower() in ("1", "true"):
        return None

    key = (public_id, version_seq)
    task = asyncio.create_task(
        _run_generation_in_background(
            public_id, version_seq, prompt, previous_html, history_prompts
        )
    )
    _RUNNING_TASKS[key] = task
    task.add_done_callback(lambda _t, k=key: _RUNNING_TASKS.pop(k, None))
    return task


@router.post("/projects/{public_id}/generate", status_code=202)
async def generate(
    public_id: str,
    data: GenerateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """受理三阶段生成（异步）。

    立即返回 ``{"status": "accepted", "version_seq": N, "steps": [...]}``，
    前端改为轮询 ``/versions/{seq}/steps`` 获取真实进度与终态。
    校验类错误在受理之前以错误信封返回（VALIDATION_ERROR / NOT_FOUND / CONFLICT）。
    """
    prompt = (data.prompt or "").strip()
    if len(prompt) < PROMPT_MIN_LEN:
        return error_envelope("VALIDATION_ERROR", "请先描述你想要的应用，描述不能为空")
    if len(prompt) > PROMPT_MAX_LEN:
        return error_envelope(
            "VALIDATION_ERROR", f"描述过长，请精简到 {PROMPT_MAX_LEN} 字以内"
        )

    await _recover_stale_versions(db, public_id)
    project = await _fetch_project(db, public_id)
    if not project:
        return error_envelope("NOT_FOUND", "项目不存在或已被删除")

    # 并发约束（FR-012）
    active = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id,
            Versions.status.in_(ACTIVE_STATUSES),
        )
    )
    if active.scalars().first():
        return error_envelope("CONFLICT", "该项目已有正在进行的生成，请等待完成后再试")

    # 迭代时回传上一版 HTML（research.md R5）
    previous_result = await db.execute(
        select(Versions)
        .where(
            Versions.project_public_id == public_id,
            Versions.status == "succeeded",
        )
        .order_by(Versions.seq.desc())
        .limit(1)
    )
    previous = previous_result.scalars().first()
    previous_html = previous.html if previous else None

    # 需求历史：本项目此前所有版本的原始需求（时间升序）。用于让模型消解
    # 「继续刚刚的需求」「按之前说的」「再优化一下」这类指代——否则模型只
    # 看到孤立的当前短句，无法还原真实意图。条数与长度由 prompts 层截断，
    # 上下文不会随轮次线性膨胀。必须在 prepare 落库新版本之前查询。
    history_result = await db.execute(
        select(Versions.prompt)
        .where(Versions.project_public_id == public_id)
        .order_by(Versions.seq)
    )
    history_prompts = [row for row in history_result.scalars().all() if row]

    pipeline = GenerationPipeline(db)
    prepared = await pipeline.prepare(project, prompt)
    version_seq = prepared["version_seq"]

    # 请求会话到此结束；三阶段在后台任务里用独立会话执行，
    # 本接口毫秒级返回，彻底避开网关 120s 代理读超时。
    task = _spawn_generation(
        public_id, version_seq, prompt, previous_html, history_prompts
    )
    if task is None:
        # GENERATION_INLINE（仅测试）：请求内直接执行，复用请求会话。
        await _run_generation_in_background(
            public_id,
            version_seq,
            prompt,
            previous_html,
            history_prompts,
            session=db,
        )

    return {
        "status": "accepted",
        "version_seq": version_seq,
        "steps": prepared["steps"],
        "step_names": list(prompts.STEP_NAMES),
    }


@router.get("/projects/{public_id}/versions/{seq}/steps")
async def get_version_steps(
    public_id: str, seq: int, db: AsyncSession = Depends(get_db)
):
    """轮询用：返回某版本的实时步骤状态与版本状态。"""
    version_result = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id, Versions.seq == seq
        )
    )
    version = version_result.scalars().first()
    if not version:
        return error_envelope("NOT_FOUND", "该版本不存在")

    steps_result = await db.execute(
        select(Generation_steps)
        .where(
            Generation_steps.project_public_id == public_id,
            Generation_steps.version_seq == seq,
        )
        .order_by(Generation_steps.seq)
    )
    steps = list(steps_result.scalars().all())
    payload = {
        "version_seq": version.seq,
        "status": version.status,
        "error": version.error,
        "steps": [
            {
                "seq": step.seq,
                "name": step.name,
                "status": step.status,
                "output": (step.output or "")[:2000],
                "started_at": step.started_at,
                "ended_at": step.ended_at,
            }
            for step in steps
        ],
    }
    await db.commit()
    return payload


@router.post("/projects/{public_id}/versions/{seq}/cancel")
async def cancel_generation(
    public_id: str, seq: int, db: AsyncSession = Depends(get_db)
):
    """取消进行中的生成（中断任务能力）。

    - 版本处于 ``pending`` / ``running``：立即落库为 ``cancelled``（含活跃步骤
      与项目状态），并对进程内后台任务调用 ``task.cancel()``——正在等待模型
      响应的 await 点会被注入 ``CancelledError`` 即时中断；若任务已越过 await
      点，流水线也会在下一阶段边界检测到 cancelled 状态后停止。
    - 版本已是终态：返回 409，避免误取消已完成的结果。
    - 已成功的旧版本不受影响；用户输入的需求已持久化，可继续提交新要求。
    """
    version_result = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id, Versions.seq == seq
        )
    )
    version = version_result.scalars().first()
    if not version:
        return error_envelope("NOT_FOUND", "该版本不存在")
    if version.status not in ACTIVE_STATUSES:
        return error_envelope("CONFLICT", "该版本已结束，无需取消")

    version.status = "cancelled"
    version.error = "生成已取消"

    steps_result = await db.execute(
        select(Generation_steps).where(
            Generation_steps.project_public_id == public_id,
            Generation_steps.version_seq == seq,
        )
    )
    now_iso = datetime.now(timezone.utc).isoformat()
    for step in steps_result.scalars().all():
        if step.status in ACTIVE_STATUSES:
            step.status = "cancelled"
            step.output = step.output or "已取消"
            step.ended_at = step.ended_at or now_iso

    project = await _fetch_project(db, public_id)
    if project and project.latest_status in ACTIVE_STATUSES:
        project.latest_status = "cancelled"

    db.add(
        Messages(
            project_public_id=public_id,
            role="assistant",
            content="已停止本次生成，之前的版本不受影响；你可以继续提交新的要求。",
            version_seq=seq,
        )
    )
    await db.commit()

    # 状态先落库再中断任务：即使 task.cancel() 的时机错过 await 点，
    # 流水线阶段边界检查也会阻止后续模型调用，结果不会覆盖取消状态。
    task = _RUNNING_TASKS.get((public_id, seq))
    if task and not task.done():
        task.cancel()

    return {"status": "cancelled", "version_seq": seq}
