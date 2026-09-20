"""心跳与 stale recovery 测试（设计文档 S3.2 / S1.2，验收 A3 补充）。

时间常量关系必须成立（前端轮询上限 12min 在 lib/constants 侧）：
HEARTBEAT_INTERVAL(30s) < STALE_AFTER(10min) < 轮询上限(12min)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.database import db_manager
from models.generation_steps import Generation_steps
from models.projects import Projects
from models.versions import Versions
from routers import atoms as atoms_module
from services import pipeline as pipeline_module
from services.pipeline import GenerationPipeline


def _naive_utc_ago(seconds: float) -> datetime:
    """SQLite 取回为 naive datetime；写入侧统一用 naive-UTC 保证可比。"""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds)


def test_timing_constants_ordering():
    assert pipeline_module.HEARTBEAT_INTERVAL == 30.0
    assert pipeline_module.STAGE_TIMEOUT == 240.0
    assert atoms_module.STALE_AFTER == timedelta(minutes=10)
    assert pipeline_module.HEARTBEAT_INTERVAL < atoms_module.STALE_AFTER.total_seconds()
    assert atoms_module.STALE_AFTER.total_seconds() < 12 * 60


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


async def test_heartbeat_refreshes_updated_at(db_session, shared_session_maker, monkeypatch):
    """心跳用独立会话周期刷新 versions.updated_at；cancelled 版本不再写活。"""
    monkeypatch.setattr(pipeline_module, "HEARTBEAT_INTERVAL", 0.05)

    async with db_manager.session() as session:
        session.add(
            Projects(
                public_id="hb-pid", title="t", owner_key="anon:x",
                version_count=1, latest_status="running", is_demo=False,
            )
        )
        session.add(
            Versions(
                project_public_id="hb-pid", seq=1, prompt="p",
                status="running", updated_at=_naive_utc_ago(3600),
            )
        )
        await session.commit()

    pipeline = GenerationPipeline(db_session, ai=object())
    task = asyncio.create_task(pipeline._heartbeat("hb-pid", 1))
    await asyncio.sleep(0.3)  # 至少 4 个心跳周期

    async with db_manager.session() as session:
        version = (
            await session.execute(
                Versions.__table__.select().where(
                    Versions.project_public_id == "hb-pid"
                )
            )
        ).first()
        age = datetime.now(timezone.utc).replace(tzinfo=None) - version.updated_at
        assert age < timedelta(seconds=5)  # 已被刷新到当下

    # 版本转 cancelled 后，迟到的心跳不得把它写回活跃时间线
    async with db_manager.session() as session:
        row = (
            await session.execute(
                Versions.__table__.select().where(
                    Versions.project_public_id == "hb-pid"
                )
            )
        ).first()
        target = (
            await session.get(Versions, row.id)
        )
        target.status = "cancelled"
        target.updated_at = _naive_utc_ago(3600)
        await session.commit()

    await asyncio.sleep(0.2)
    async with db_manager.session() as session:
        after = (
            await session.execute(
                Versions.__table__.select().where(
                    Versions.project_public_id == "hb-pid"
                )
            )
        ).first()
        age = datetime.now(timezone.utc).replace(tzinfo=None) - after.updated_at
        assert age > timedelta(minutes=59)  # 心跳对已取消版本停写

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
