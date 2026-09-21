"""stale recovery 测试（设计文档 S3.2 / S1.2，验收 A3 补充）。

时间常量关系（``STAGE_TIMEOUT`` < ``GENERATION_BUDGET_SECONDS`` < ``STALE_AFTER``
< 前端轮询上限）由 ``tests/test_time_budget_invariants.py`` 守护——那里会从
``Index.tsx`` 读前端真实值。本文件只负责回收行为本身。

原 ``test_timing_constants_ordering`` 因引用已删除的 ``HEARTBEAT_INTERVAL``
而移除，其覆盖范围已被上述不变量测试完整取代（且那边不止断言排序，还断言
「单阶段全部重试的最坏耗时必须落在总预算内」这一修复前的核心漏洞）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.database import db_manager
from fakes import ANALYSIS_JSON, DESIGN_JSON, FakeAIHub, make_html
from models.generation_steps import Generation_steps
from models.projects import Projects
from models.versions import Versions
from routers import atoms as atoms_module
from services import pipeline as pipeline_module
from services.pipeline import GenerationPipeline


def _naive_utc_ago(seconds: float) -> datetime:
    """构造「若干秒前」的 naive 时间戳，口径与生产写入**一致**。

    关键：模型里 ``created_at`` / ``updated_at`` 的默认值是 ``datetime.now``
    （本地时间，naive），所以真实落库的 naive 时间戳是**本地时间**而非 UTC。
    此前这里用 naive-UTC 播种，在 UTC-7 的运行环境下与生产差 7 小时——测试
    自己造了一份现实中不存在的数据，既可能掩盖也可能伪造 stale 判定。改用
    ``datetime.now()`` 保持与写入侧同口径。
    """
    return datetime.now() - timedelta(seconds=seconds)


async def _seed_running_version(pid: str, seconds_ago: float) -> None:
    async with db_manager.session() as session:
        # 项目状态同步置为 running，与真实生成受理后的状态一致，
        # 使 stale 恢复后的 latest_status 断言有意义。
        project = (
            await session.execute(
                select(Projects).where(Projects.public_id == pid)
            )
        ).scalars().first()
        project.latest_status = "running"
        session.add(
            Versions(
                project_public_id=pid,
                seq=1,
                prompt="进行中的需求",
                status="running",
                updated_at=_naive_utc_ago(seconds_ago),
            )
        )
        for idx, name in enumerate(("需求分析", "结构设计", "代码生成"), start=1):
            session.add(
                Generation_steps(
                    project_public_id=pid,
                    version_seq=1,
                    seq=idx,
                    name=name,
                    status="running" if idx == 1 else "pending",
                )
            )
        await session.commit()


async def _make_project(client) -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": "t"})
    return response.json()["public_id"]


async def test_stale_running_version_recovered(http):
    """超过 STALE_AFTER 无心跳的 running 版本：详情接口把它落为 failed。"""
    client = http()
    async with client:
        pid = await _make_project(client)
        await _seed_running_version(pid, seconds_ago=30 * 60)

        detail = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
        version = detail["versions"][0]
        assert version["status"] == "failed"
        assert "中断" in version["error"]  # 面向用户的可读中文
        assert all(s["status"] == "failed" for s in version["steps"])
        assert detail["latest_status"] == "failed"


async def test_fresh_running_version_not_killed(http):
    """心跳正常（updated_at 新鲜）的进行中版本不被 stale 判定误杀。"""
    client = http()
    async with client:
        pid = await _make_project(client)
        await _seed_running_version(pid, seconds_ago=60)

        detail = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
        assert detail["versions"][0]["status"] == "running"


async def test_stale_recovery_is_owner_scoped(http):
    """stale 恢复按归属限定：访客 B 的任意请求不得改动访客 A 的版本。"""
    a, b = http(), http()
    async with a, b:
        pid = await _make_project(a)
        await _seed_running_version(pid, seconds_ago=30 * 60)

        # B 列项目 + 尝试访问 A 的项目（404），都不应触发恢复
        await b.get("/api/v1/atoms/projects")
        assert (await b.get(f"/api/v1/atoms/projects/{pid}")).status_code == 404

        async with db_manager.session() as session:
            version = (
                await session.execute(
                    Versions.__table__.select().where(
                        Versions.project_public_id == pid
                    )
                )
            ).first()
            assert version.status == "running"  # 仍是 running，未被 B 改动

        # A 自己的请求才触发恢复
        detail = (await a.get(f"/api/v1/atoms/projects/{pid}")).json()
        assert detail["versions"][0]["status"] == "failed"


async def test_steps_endpoint_recovers_stale_and_allows_retry(http, inject_fake_ai):
    """轮询端点自身也要回收陈旧版本，且回收必须写入**非空**的 ``error_type``。

    contracts/rest-api.md 变更 1 + 4。为什么必须挂在 steps 端点上：刷新后的前端
    处在「进行中」状态时**只轮询这个端点**，回收若只挂在列表/详情上，一个刷新后
    不再经过列表的会话会永久显示「生成中」。``error_type`` 为空则让前端无法区分
    「超出时间预算」「上游不可用」与「被中断」——三种都要求不同的用户动作。
    """
    client = http()
    async with client:
        pid = await _make_project(client)
        await _seed_running_version(pid, seconds_ago=30 * 60)

        snap = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/1/steps")
        ).json()

        # ← 这两条是本次修复的红旗：旧实现里 steps 端点不做回收
        assert snap["status"] == "failed", "steps 端点未触发 stale 回收"
        assert snap["error_type"], "回收路径写入了空的 error_type"

        assert "Traceback" not in (snap["error"] or "")
        assert all(s["status"] != "running" for s in snap["steps"])

        # 回收不得新建版本（版本数从库直读，避免再经端点触发一次回收）
        async with db_manager.session() as session:
            rows = (
                await session.execute(
                    select(Versions).where(Versions.project_public_id == pid)
                )
            ).scalars().all()
        assert len(rows) == 1, "回收过程新建了版本"
        assert rows[0].status == "failed"

        # FR-019：失败之后必须能**立即**再次提交，不被 409 挡住
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("重来")]))
        retry = await client.post(
            f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": "重新提交一次"}
        )
        assert retry.status_code == 202, retry.text


async def test_version_updated_at_only_advances_at_real_progress_points(
    db_session, shared_session_maker
):
    """``versions.updated_at`` 只在真实进展点推进，不由任何后台任务刷新。

    这条替代了原先的 ``test_heartbeat_refreshes_updated_at``：心跳已删除
    （理由见 ``services/pipeline.py`` 末尾注释——它刷新的正是回收判据所用的
    ``updated_at``，与回收机制逻辑互斥）。这里正面锁定替代方案所依赖的事实：
    没有进程在后台推进 ``versions.updated_at``，所以静默只能由流水线自己的
    进展点（``_finish_step`` / 终态落库）打断。
    """
    async with db_manager.session() as session:
        session.add(
            Projects(
                public_id="no-hb-pid", title="t", owner_key="anon:x",
                version_count=1, latest_status="running", is_demo=False,
            )
        )
        session.add(
            Versions(
                project_public_id="no-hb-pid", seq=1, prompt="p",
                status="running", updated_at=_naive_utc_ago(3600),
            )
        )
        await session.commit()

    # 给「可能存在的后台刷新」充分的时间窗（原心跳周期 30s 的 1/100 已是 0.3s，
    # 这里给到 0.3s 足以跑出任何 0.05s 级的周期任务）
    await asyncio.sleep(0.3)

    async with db_manager.session() as session:
        row = (
            await session.execute(
                Versions.__table__.select().where(
                    Versions.project_public_id == "no-hb-pid"
                )
            )
        ).first()
        age = datetime.now(timezone.utc).replace(tzinfo=None) - row.updated_at
        assert age > timedelta(minutes=59), (
            "versions.updated_at 被某个后台任务刷新了——心跳不得复活"
        )
