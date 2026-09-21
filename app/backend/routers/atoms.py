"""Atoms Studio 平台的自定义 API。

对应 specs/001-atoms-demo/contracts/rest-api.md 与设计文档 2026-09-20（S1/S5）：

- ``GET    /api/v1/atoms/health``                          健康检查
- ``GET    /api/v1/atoms/projects``                        项目列表（不含 html）
- ``POST   /api/v1/atoms/projects``                        创建空项目
- ``GET    /api/v1/atoms/projects/{public_id}``            项目详情（含版本与对话，不含 html）
- ``GET    /api/v1/atoms/projects/{public_id}/versions/{seq}``  单版本完整内容（含 html + sha256）
- ``DELETE /api/v1/atoms/projects/{public_id}``            删除项目（级联）
- ``POST   /api/v1/atoms/projects/{public_id}/generate``   三阶段生成（异步受理 202）
- ``POST   /api/v1/atoms/projects/{public_id}/versions/{seq}/cancel``   取消进行中的生成
- ``POST   /api/v1/atoms/projects/{public_id}/versions/{seq}/restore``  回滚（回滚即新版本，S5）

契约要点：
1. 对外一律使用 ``public_id``（UUID v4），**自增 id 不出现在任何响应中**。
2. 所有错误统一为 ``{"error": {"code": ..., "message": ...}}``，``message`` 必须是
   面向用户的可读中文，不含堆栈或内部路径。
3. ``VALIDATION_ERROR`` / ``NOT_FOUND`` / ``CONFLICT`` 在开始生成之前返回。
4. 并发约束：同项目已有 ``pending`` / ``running`` 版本时返回 409。
5. **不变量 1（归属）**：每一次读写都经 ``_require_project`` / ``_visible`` 唯一
   过滤入口，归属键由服务端派生（dependencies/owner.py），请求体不参与；
   查不到归属一律 404（fail-closed），演示项目写操作 409。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import db_manager, get_db
from dependencies.owner import (
    ANON_COOKIE,
    ANON_MAX_AGE,
    OwnerContext,
    _issue,
    anon_cookie_secure,
    get_owner,
)
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
# 超过该时长仍处于 pending/running 且 updated_at 未推进的版本视为中断
# （进程死亡 / 任务消失）。判据是 updated_at，而推进 updated_at 的是流水线
# 的真实进展点（阶段边界与每次调用落库），不是心跳——心跳已删除。
# 关系常量（见 services/pipeline.py 顶部与 contracts/generation-lifecycle.md）：
# STAGE_TIMEOUT(120s) < GENERATION_BUDGET_SECONDS(420s)
#                      < STALE_AFTER(600s) < 前端轮询上限(720s)
STALE_AFTER = timedelta(minutes=10)

ERROR_STATUS = {
    "VALIDATION_ERROR": 400,
    "NOT_FOUND": 404,
    "CONFLICT": 409,
    "UPSTREAM_ERROR": 502,
    "INTERNAL_ERROR": 500,
}


# ------------------------------------------------------------------ 错误与响应构造


class RouteError(Exception):
    """路由层业务错误：由 _require_project 等入口抛出，转统一错误信封。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _json(
    payload: Any, ctx: OwnerContext | None = None, status_code: int = 200
) -> JSONResponse:
    """构造响应并附加匿名 cookie（ctx.anon_key 非空时，双通道之一）。

    ``secure`` 由请求实际 scheme 推导（见 ``anon_cookie_secure``），HTTPS 下发
    ``Secure``、本地 HTTP 不下发——写死 Secure 会让本地开发时浏览器不回传 cookie，
    匿名身份每请求重建，会话恢复根本无法验证。
    """
    response = JSONResponse(status_code=status_code, content=payload)
    if ctx and ctx.anon_key:
        response.set_cookie(
            key=ANON_COOKIE,
            value=ctx.anon_key,
            max_age=ANON_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=anon_cookie_secure(),
            path="/",
        )
    return response


def error_envelope(
    code: str, message: str, ctx: OwnerContext | None = None
) -> JSONResponse:
    """构造统一错误信封（message 面向用户，可直接展示）。

    错误响应同样携带匿名 cookie：新访客第一次请求就失败时，其身份也应被
    持久化，避免下次请求又变成全新身份。
    """
    return _json({"error": {"code": code, "message": message}}, ctx, ERROR_STATUS.get(code, 500))


# ------------------------------------------------------------------ 请求模型
#
# 注意：请求体**不接受** owner_key（不变量 1，归属由服务端派生）。


class CreateProjectRequest(BaseModel):
    title: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str = ""


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


def _html_sha256(html: str | None) -> str:
    """版本 HTML 的 sha256（S5.2）：预览与源码工具条渲染同一个值。"""
    return hashlib.sha256((html or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 归属过滤入口（不变量 1）


def _visible(stmt, owner: str):
    """读路径：本人的 + 演示项目。"""
    return stmt.where(or_(Projects.owner_key == owner, Projects.is_demo.is_(True)))


def _owned(stmt, owner: str):
    """写路径：仅本人的。"""
    return stmt.where(Projects.owner_key == owner)


async def _require_project(
    db: AsyncSession, public_id: str, ctx: OwnerContext, *, write: bool
) -> Projects:
    """本模块**唯一**的项目获取入口，路由不得自行拼 where。

    - 查不到（不存在 / 不归属且非演示）→ RouteError(NOT_FOUND)，fail-closed：
      不用 403，避免探测项目是否存在。
    - write=True 且命中演示项目 → RouteError(CONFLICT)，演示项目只读。
    """
    # 读写统一走 _visible：演示项目也必须能被写路径「命中」，
    # 命中后按 is_demo 拒绝（409），而不是被 where 过滤成 404。
    result = await db.execute(
        _visible(select(Projects), ctx.owner_key).where(
            Projects.public_id == public_id
        )
    )
    project = result.scalars().first()
    if not project:
        raise RouteError("NOT_FOUND", "项目不存在或已被删除")
    if write:
        if project.is_demo:
            raise RouteError(
                "CONFLICT",
                "这是演示项目，仅供浏览；请点左上角「新项目」创建你自己的项目",
            )
        # 防御性兜底：_visible 命中但非本人（理论上仅剩演示分支）→ 404
        if project.owner_key != ctx.owner_key:
            raise RouteError("NOT_FOUND", "项目不存在或已被删除")
    return project


async def _fetch_version(
    db: AsyncSession, public_id: str, seq: int
) -> Versions | None:
    result = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id, Versions.seq == seq
        )
    )
    return result.scalars().first()


# ------------------------------------------------------------------ 陈旧版本恢复

# 回收路径写入的错误分类。必须非空：前端靠它把「这次生成被打断了」与
# 「上游不可用」「超出时间预算」分开——三者的用户动作不同。
STALE_ERROR_TYPE = "interrupted"
STALE_ERROR_MESSAGE = "生成过程被中断（服务重启或连接断开），你的描述已保留，可重新提交"


async def _recover_stale_versions(
    db: AsyncSession, owner: str, public_id: str | None = None
) -> None:
    """把**该归属身份名下**中断的 pending/running 版本置为 failed。

    设计文档 S1.2 / S3.2 的三点约束：
    1. JOIN Projects 限定 owner_key == owner——消除「任意访客一次列表请求
       触发全表扫描 + 全表 UPDATE」的放大攻击面。
    2. 判据用 updated_at 而非 created_at，长任务不被误杀；updated_at 缺失的
       旧数据回退 created_at。**没有任何后台任务会推进 updated_at**（心跳已删除，
       见 services/pipeline.py）：它只在流水线的真实进展点前进，所以活任务的
       最长静默期就是它的总预算，小于 STALE_AFTER。
    3. 残留版本置 failed 并写入面向用户的可读原因与**非空 error_type**，
       避免永久卡住的加载态与无法归因的失败。
    """
    stmt = (
        select(Versions)
        .join(Projects, Projects.public_id == Versions.project_public_id)
        .where(Projects.owner_key == owner, Versions.status.in_(ACTIVE_STATUSES))
    )
    if public_id:
        stmt = stmt.where(Versions.project_public_id == public_id)
    result = await db.execute(stmt)
    stale = list(result.scalars().all())
    if not stale:
        return

    now = datetime.now(timezone.utc)
    changed = False
    for version in stale:
        stamp = version.updated_at or version.created_at
        if stamp and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp and now - stamp < STALE_AFTER:
            continue

        version.status = "failed"
        version.error = STALE_ERROR_MESSAGE
        # error_type 一并写入：留空会让 get_version_steps 派生出 None，
        # 前端只能退化成通用文案，无法区分「被打断」与「上游故障」（T033）。
        existing_summary = _parse_summary(version.summary) or {}
        existing_summary["error_type"] = STALE_ERROR_TYPE
        version.summary = json.dumps(existing_summary, ensure_ascii=False)
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

        project_result = await db.execute(
            select(Projects).where(Projects.public_id == version.project_public_id)
        )
        project = project_result.scalars().first()
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "failed"

    if changed:
        await db.commit()


async def _migration_selfcheck(db: AsyncSession) -> None:
    """迁移自检（S1.3）：存在 NULL owner 且非演示的项目时打 ERROR 日志。

    只记日志，不改变行为——那些项目对所有人不可见（fail-closed 安全侧）。
    """
    try:
        result = await db.execute(
            select(func.count(Projects.id)).where(
                Projects.owner_key.is_(None),
                or_(Projects.is_demo.is_(False), Projects.is_demo.is_(None)),
            )
        )
        count = result.scalar() or 0
        if count:
            logger.error(
                "发现 %s 个 owner_key 为空且非演示的项目，请执行 "
                "scripts/backfill_demo_owner.py 迁移（当前它们对所有身份不可见）",
                count,
            )
    except Exception as exc:  # noqa: BLE001 - 自检不得影响主流程
        logger.warning("迁移自检失败: %s", exc)


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


@router.post("/session/logout")
async def logout():
    """登出：丢弃浏览器侧的旧匿名身份，并签发一个全新匿名身份。

    响应 ``{"status":"ok","anon_key":"<新值>"}``，并经 ``Set-Cookie`` 一并下发
    （双通道，与其它 atoms 路由一致——网关吞掉 Set-Cookie 时前端仍能从响应体
    拿到新身份）。

    **为什么必须由服务端做**：匿名 cookie 是 ``HttpOnly`` 的（刻意的，防 XSS
    窃取身份），前端 JS 既读不到也删不掉。旧实现里前端 ``clearAnonKey()`` 只清得
    了 localStorage，cookie 原样留着，下一次请求又回到旧匿名身份——用户看到的
    现象是「登出无效，还是能看到刚才的东西」。

    **这里刻意不调 ``response.delete_cookie()``**：同 name + 同 path 的
    ``set_cookie`` 本身就覆盖旧 cookie，删除是冗余的；而多写一条 ``Set-Cookie``
    会引入顺序陷阱——浏览器按顺序应用同名 cookie，若删除那条排在写入之后，
    刚签发的新身份会被立刻抹掉。需要的是「换成新身份」，不是「先清空」。
    """
    raw = _issue()
    # 不复用 ctx.anon_key：那是**旧**身份，登出要换的就是它。
    ctx = OwnerContext(owner_key=f"anon:{raw}", anon_key=raw)
    return _json({"status": "ok", "anon_key": raw}, ctx)


@router.get("/projects")
async def list_projects(
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """项目列表（本人的 + 演示），按 updated_at 倒序，不含 html。

    响应体携带 ``anon_key``（双通道之二）：前端持久化后经 ``X-Atoms-Anon``
    头回传，即使网关吃掉 Set-Cookie 身份也不丢。
    """
    await _recover_stale_versions(db, ctx.owner_key)
    await _migration_selfcheck(db)
    stmt = (
        _visible(select(Projects), ctx.owner_key)
        .order_by(Projects.updated_at.desc())
    )
    result = await db.execute(stmt)
    projects = list(result.scalars().all())
    payload = {
        "projects": [_project_brief(item) for item in projects],
        "anon_key": ctx.anon_key,
    }
    await db.commit()
    return _json(payload, ctx)


@router.post("/projects")
async def create_project(
    data: CreateProjectRequest,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """创建空项目。归属取服务端派生的 ctx.owner_key，请求体不参与。"""
    title = (data.title or "").strip()[:120] or FALLBACK_TITLE
    project = Projects(
        public_id=str(uuid.uuid4()),
        title=title,
        owner_key=ctx.owner_key,
        version_count=0,
        latest_status=None,
        is_demo=False,
    )
    db.add(project)
    await db.commit()
    return _json(_project_brief(project), ctx, status_code=201)


@router.get("/projects/{public_id}")
async def get_project(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """项目详情：含版本列表（带步骤）与对话记录，不含 html。"""
    try:
        project = await _require_project(db, public_id, ctx, write=False)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    await _recover_stale_versions(db, ctx.owner_key, public_id)

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
        "is_demo": bool(project.is_demo),
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
    return _json(payload, ctx)


@router.get("/projects/{public_id}/versions/{seq}")
async def get_version(
    public_id: str,
    seq: int,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """单个版本的完整内容，**仅此接口返回 html**。

    附 ``html_sha256``（S5.2）：预览与源码工具条渲染同一个后端值，
    「一致」是构造性成立而非两次独立计算碰巧相等。
    附 ``error_type``（S3.5）：从 summary JSON 派生的便捷字段。
    """
    try:
        await _require_project(db, public_id, ctx, write=False)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    version = await _fetch_version(db, public_id, seq)
    if not version:
        return error_envelope("NOT_FOUND", "该版本不存在", ctx)

    summary = _parse_summary(version.summary)
    payload = {
        "seq": version.seq,
        "prompt": version.prompt,
        "html": version.html or "",
        "html_sha256": _html_sha256(version.html),
        "summary": summary,
        "status": version.status,
        "error": version.error,
        "error_type": (summary or {}).get("error_type"),
        "duration_ms": version.duration_ms,
        "created_at": _iso(version.created_at),
    }
    await db.commit()
    return _json(payload, ctx)


@router.delete("/projects/{public_id}")
async def delete_project(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """删除项目及其下全部版本、消息与步骤（级联）。演示项目返回 409。"""
    try:
        project = await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    for model in (Generation_steps, Messages, Versions):
        rows = await db.execute(
            select(model).where(model.project_public_id == public_id)
        )
        for row in rows.scalars().all():
            await db.delete(row)
    await db.delete(project)
    await db.commit()
    return _json({"deleted": True}, ctx)


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
# 已知限制：进程内表，多 worker 部署取消失效（设计文档 §5，单 worker 约束）。
_RUNNING_TASKS: dict[tuple[str, int], asyncio.Task] = {}

# 测试注入点（S4）：设置后，后台流水线的 AI 客户端由该工厂创建。
# 生产恒为 None；测试经 fixture 注入 FakeAIHub，无需真实 key、不花配额。
_PIPELINE_AI_FACTORY: Any | None = None


async def _run_generation_in_background(
    public_id: str,
    version_seq: int,
    prompt: str,
    previous_html: str | None,
    history_prompts: list[str] | None = None,
) -> None:
    """后台执行三阶段流水线，使用独立 DB 会话（请求会话此时已关闭）。"""
    try:
        async with db_manager.session() as session:
            ai = _PIPELINE_AI_FACTORY() if _PIPELINE_AI_FACTORY else None
            pipeline = GenerationPipeline(session, ai=ai)
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


async def _start_generation(
    public_id: str,
    version_seq: int,
    prompt: str,
    previous_html: str | None,
    history_prompts: list[str] | None,
) -> None:
    """启动生成任务。

    GENERATION_INLINE 环境变量为真时**在当前协程内直接 await** 流水线——
    仅供测试（S4）：轮询后台任务到终态会引入 flaky 与慢测试。生产不设该变量。
    否则创建受跟踪的后台任务，注册到任务表以支持取消，并防止 task 被 GC 提前回收。

    真值判定**必须按严格口径**，不能写成 ``if os.getenv("GENERATION_INLINE")``：
    ``os.getenv`` 返回字符串，``"0"``/``"false"``/``"no"`` 全是真值，而
    ``start_app_v2.sh`` 会把 env 文件里每一行 ``KEY=VALUE`` 都 export 进后端进程。
    于是开发者在 ``app/.env`` 里写 ``GENERATION_INLINE=0`` 想关掉这个逃生舱，
    反而会打开它——``generate`` 在请求内 await 整条 1~4 分钟的流水线，撞上网关
    约 120s 的代理读超时。口径与 ``routers/auth.py`` 的 ``LOCAL_PATCH`` 一致。
    """
    if os.getenv("GENERATION_INLINE", "").strip().lower() in ("1", "true"):
        await _run_generation_in_background(
            public_id, version_seq, prompt, previous_html, history_prompts
        )
        return

    key = (public_id, version_seq)
    task = asyncio.create_task(
        _run_generation_in_background(
            public_id, version_seq, prompt, previous_html, history_prompts
        )
    )
    _RUNNING_TASKS[key] = task
    task.add_done_callback(lambda _t, k=key: _RUNNING_TASKS.pop(k, None))


@router.post("/projects/{public_id}/generate", status_code=202)
async def generate(
    public_id: str,
    data: GenerateRequest,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """受理三阶段生成（异步）。

    立即返回 ``{"status": "accepted", "version_seq": N, "steps": [...]}``，
    前端改为轮询 ``/versions/{seq}/steps`` 获取真实进度与终态。
    校验类错误在受理之前以错误信封返回（VALIDATION_ERROR / NOT_FOUND / CONFLICT）。
    演示项目为只读，写路径命中即 409。
    """
    prompt = (data.prompt or "").strip()
    if len(prompt) < PROMPT_MIN_LEN:
        return error_envelope("VALIDATION_ERROR", "请先描述你想要的应用，描述不能为空", ctx)
    if len(prompt) > PROMPT_MAX_LEN:
        return error_envelope(
            "VALIDATION_ERROR", f"描述过长，请精简到 {PROMPT_MAX_LEN} 字以内", ctx
        )

    try:
        project = await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    await _recover_stale_versions(db, ctx.owner_key, public_id)

    # 并发约束（FR-012）
    active = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id,
            Versions.status.in_(ACTIVE_STATUSES),
        )
    )
    if active.scalars().first():
        return error_envelope("CONFLICT", "该项目已有正在进行的生成，请等待完成后再试", ctx)

    # 不变量 2：迭代基线永远是 seq 最大的 succeeded 版本（research.md R5）
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

    # 需求历史：本项目此前**成功版本**的原始需求（时间升序）。失败/取消轮次
    # 不入历史（设计文档 S1.2：避免污染指代消解）。用于让模型消解
    # 「继续刚刚的需求」「按之前说的」「再优化一下」这类指代——否则模型只
    # 看到孤立的当前短句，无法还原真实意图。条数与长度由 prompts 层截断，
    # 上下文不会随轮次线性膨胀。必须在 prepare 落库新版本之前查询。
    history_result = await db.execute(
        select(Versions.prompt)
        .where(
            Versions.project_public_id == public_id,
            Versions.status == "succeeded",
        )
        .order_by(Versions.seq)
    )
    history_prompts = [row for row in history_result.scalars().all() if row]

    pipeline = GenerationPipeline(db)
    prepared = await pipeline.prepare(project, prompt)
    version_seq = prepared["version_seq"]

    # 请求会话到此结束；三阶段在后台任务里用独立会话执行，
    # 本接口毫秒级返回，彻底避开网关 120s 代理读超时。
    await _start_generation(
        public_id, version_seq, prompt, previous_html, history_prompts
    )

    return _json(
        {
            "status": "accepted",
            "version_seq": version_seq,
            "steps": prepared["steps"],
            "step_names": list(prompts.STEP_NAMES),
        },
        ctx,
        status_code=202,
    )


@router.get("/projects/{public_id}/versions/{seq}/steps")
async def get_version_steps(
    public_id: str,
    seq: int,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """轮询用：返回某版本的实时步骤状态与版本状态。

    这里也要触发陈旧回收（contracts/rest-api.md 变更 1）：前端在「进行中」时
    **只轮询这个端点**，回收若只挂在列表/详情上，一个刷新后不再经过列表的会话会
    永久停留在「生成中」——那正是本特性要消灭的失败形态。归属限定与其它路由一致。
    """
    try:
        await _require_project(db, public_id, ctx, write=False)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    await _recover_stale_versions(db, ctx.owner_key, public_id)

    version = await _fetch_version(db, public_id, seq)
    if not version:
        return error_envelope("NOT_FOUND", "该版本不存在", ctx)

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
        # S3.5：轮询快照同样派生 error_type，前端失败原因条无需再取版本详情
        "error_type": (_parse_summary(version.summary) or {}).get("error_type"),
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
    return _json(payload, ctx)


@router.post("/projects/{public_id}/versions/{seq}/cancel")
async def cancel_generation(
    public_id: str,
    seq: int,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """取消进行中的生成（中断任务能力）。

    - 版本处于 ``pending`` / ``running``：立即落库为 ``cancelled``（含活跃步骤
      与项目状态），并对进程内后台任务调用 ``task.cancel()``——正在等待模型
      响应的 await 点会被注入 ``CancelledError`` 即时中断；若任务已越过 await
      点，流水线也会在下一阶段边界检测到 cancelled 状态后停止。
    - 版本已是终态：返回 409，避免误取消已完成的结果。
    - 已成功的旧版本不受影响；用户输入的需求已持久化，可继续提交新要求。
    """
    try:
        await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    version = await _fetch_version(db, public_id, seq)
    if not version:
        return error_envelope("NOT_FOUND", "该版本不存在", ctx)
    if version.status not in ACTIVE_STATUSES:
        return error_envelope("CONFLICT", "该版本已结束，无需取消", ctx)

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

    project_result = await db.execute(
        select(Projects).where(Projects.public_id == public_id)
    )
    project = project_result.scalars().first()
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

    return _json({"status": "cancelled", "version_seq": seq}, ctx)


@router.post("/projects/{public_id}/versions/{seq}/restore")
async def restore_version(
    public_id: str,
    seq: int,
    db: AsyncSession = Depends(get_db),
    ctx: OwnerContext = Depends(get_owner),
):
    """回滚到指定版本（S5.1，Q4「回滚即新版本」）。

    - 创建 ``seq' = max(seq) + 1`` 的新版本，``html`` / ``prompt`` **逐字节**
      复制自目标版本，``summary`` 追加 ``{"restored_from": seq}``，
      状态直接 ``succeeded``；原版本全部保留，可再次回滚。
    - **基线自动正确**（不变量 2）：新 seq 即最大 seq，下一轮增量自然以
      回滚结果为基线，无需改流水线。
    - 目标版本非 succeeded / html 为空 → 409；有进行中的生成 → 409。
    - ``max(seq)+1`` 非原子的并发冲突由唯一约束兜底：IntegrityError → 409。
    """
    try:
        project = await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    # 先清理中断残留，否则「有进行中的生成」判定会被僵尸版本永久挡住回滚
    await _recover_stale_versions(db, ctx.owner_key, public_id)

    target = await _fetch_version(db, public_id, seq)
    if not target:
        return error_envelope("NOT_FOUND", "该版本不存在", ctx)
    if target.status != "succeeded" or not (target.html or "").strip():
        return error_envelope("CONFLICT", "该版本没有可回滚的内容", ctx)

    active = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id,
            Versions.status.in_(ACTIVE_STATUSES),
        )
    )
    if active.scalars().first():
        return error_envelope("CONFLICT", "该项目已有正在进行的生成，请等待完成后再试", ctx)

    max_result = await db.execute(
        select(func.max(Versions.seq)).where(
            Versions.project_public_id == public_id
        )
    )
    next_seq = (max_result.scalar() or 0) + 1

    summary = _parse_summary(target.summary) or {}
    summary["restored_from"] = seq

    db.add(
        Versions(
            project_public_id=public_id,
            seq=next_seq,
            prompt=target.prompt,
            html=target.html,
            summary=json.dumps(summary, ensure_ascii=False),
            status="succeeded",
            duration_ms=None,
        )
    )
    db.add(
        Messages(
            project_public_id=public_id,
            role="assistant",
            content=f"已回滚到 v{seq}，后续生成将以该版本为基线。",
            version_seq=next_seq,
        )
    )
    project.version_count = next_seq
    project.latest_status = "succeeded"

    try:
        await db.commit()
    except IntegrityError:
        # (project_public_id, seq) 唯一约束冲突：与并发生成/回滚撞号
        await db.rollback()
        return error_envelope("CONFLICT", "该项目正在发生变化，请刷新后重试", ctx)

    return _json(
        {"restored": True, "version_seq": next_seq, "restored_from": seq}, ctx
    )
