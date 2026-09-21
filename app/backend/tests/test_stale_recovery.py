"""stale recovery 测试（设计文档 S1.2 / S3.2；002-harden-increment-session T006/T007）。

心跳已删除：``versions.updated_at`` 只在真实进展点推进（阶段完成写摘要、
失败/成功收尾写终态），因此进行中任务的最长静默期 = 整体预算。
时间常量关系必须成立（前端轮询上限 12min 在 lib/constants 侧）：
GENERATION_BUDGET_SECONDS(420s) < STALE_AFTER(10min) < 轮询上限(12min)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.database import db_manager
from models.generation_steps import Generation_steps
from models.projects import Projects
from models.versions import Versions
from routers import atoms as atoms_module
from services import pipeline as pipeline_module


def _naive_utc_ago(seconds: float) -> datetime:
    """SQLite 取回为 naive datetime；写入侧统一用 naive-UTC 保证可比。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)


def test_timing_constants_ordering():
    """预算内活任务不被 stale 误杀的常量关系守护（心跳删除后的新语义）。"""
    assert pipeline_module.STAGE_TIMEOUT == 120.0
    assert pipeline_module.GENERATION_BUDGET_SECONDS == 420.0
    assert atoms_module.STALE_AFTER == timedelta(minutes=10)
    # 心跳已删除：活任务最长静默期 = 整体预算，必须落在 stale 阈值之内
    assert (
        pipeline_module.GENERATION_BUDGET_SECONDS
        < atoms_module.STALE_AFTER.total_seconds()
    )
    assert atoms_module.STALE_AFTER.total_seconds() < 12 * 60
    # 单阶段超时 × 重试次数也必须落在整体预算内（最坏路径不会超出 deadline）
    assert (
        pipeline_module.STAGE_TIMEOUT * pipeline_module.MAX_ATTEMPTS
        < pipeline_module.GENERATION_BUDGET_SECONDS
    )


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
    """超过 STALE_AFTER 无进展的 running 版本：详情接口把它落为 failed。"""
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
    """updated_at 新鲜（真实进展点刚推进过）的进行中版本不被 stale 判定误杀。"""
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
