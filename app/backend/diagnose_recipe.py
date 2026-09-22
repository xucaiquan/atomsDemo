"""按关键词检索项目及其全部版本，定位「第二轮加图片一直超时」到底发生了什么。

不只看 pending/running：用户感知的「一直转」可能对应三种库内事实，必须区分——
1. 版本还真的是 running（后台任务仍在跑，或进程已死留下僵尸）
2. 版本早已 failed（后端止损了，但前端没收到/没提示）
3. 版本已 succeeded（生成完了，前端没接回）
三者的修法完全不同，所以这里把终态、时间戳、summary 里的 error_type 一并打出来。
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import or_, select

from core.database import db_manager
from models.generation_steps import Generation_steps
from models.projects import Projects
from models.versions import Versions

KEYWORDS = ("食谱", "菜谱", "recipe", "美食")


def _age(ts) -> str:
    if ts is None:
        return "—"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return f"({ts})"
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    delta = (datetime.now() - ts).total_seconds()
    if delta > 3600:
        return f"{delta / 3600:.1f}h 前"
    return f"{delta:.0f}s 前"


async def main() -> None:
    async with db_manager.session() as db:
        # 标题可能被阶段 1 回填成「每日食谱推荐」之类，也可能仍是占位标题，
        # 所以标题和需求文本都要搜。
        projects = (
            await db.execute(
                select(Projects)
                .where(or_(*[Projects.title.ilike(f"%{k}%") for k in KEYWORDS]))
                .order_by(Projects.updated_at.desc())
            )
        ).scalars().all()

        hit_ids = {p.public_id for p in projects}
        prompt_hits = (
            await db.execute(
                select(Versions)
                .where(or_(*[Versions.prompt.ilike(f"%{k}%") for k in KEYWORDS]))
                .order_by(Versions.updated_at.desc())
            )
        ).scalars().all()
        for v in prompt_hits:
            if v.project_public_id not in hit_ids:
                hit_ids.add(v.project_public_id)
                p = (
                    await db.execute(
                        select(Projects).where(
                            Projects.public_id == v.project_public_id
                        )
                    )
                ).scalars().first()
                if p:
                    projects.append(p)

        if not projects:
            print(f"未找到标题或需求包含 {KEYWORDS} 的项目")
            return

        for p in projects:
            print("=" * 78)
            print(f"项目: {p.title}  ({p.public_id})")
            print(
                f"latest_status={p.latest_status} version_count={p.version_count} "
                f"updated_at={p.updated_at} ({_age(p.updated_at)})"
            )
            versions = (
                await db.execute(
                    select(Versions)
                    .where(Versions.project_public_id == p.public_id)
                    .order_by(Versions.seq)
                )
            ).scalars().all()
            for v in versions:
                print("-" * 70)
                print(
                    f"  v{v.seq} status={v.status} html={len(v.html or '')} 字符 "
                    f"duration_ms={v.duration_ms}"
                )
                print(f"  需求: {(v.prompt or '')[:140]}")
                print(f"  created={v.created_at} ({_age(v.created_at)})")
                print(f"  updated={v.updated_at} ({_age(v.updated_at)})")
                print(f"  error={v.error}")
                print(f"  summary={(v.summary or '')[:500]}")
                steps = (
                    await db.execute(
                        select(Generation_steps)
                        .where(
                            Generation_steps.project_public_id == p.public_id,
                            Generation_steps.version_seq == v.seq,
                        )
                        .order_by(Generation_steps.seq)
                    )
                ).scalars().all()
                for s in steps:
                    span = "—"
                    if s.started_at and s.ended_at:
                        try:
                            a = datetime.fromisoformat(s.started_at.replace("Z", "+00:00"))
                            b = datetime.fromisoformat(s.ended_at.replace("Z", "+00:00"))
                            span = f"{(b - a).total_seconds():.1f}s"
                        except ValueError:
                            pass
                    print(
                        f"    步骤{s.seq} {s.name:<8} {s.status:<10} 耗时={span:<8} "
                        f"started={_age(s.started_at)} out={(s.output or '')[:60]!r}"
                    )


if __name__ == "__main__":
    asyncio.run(main())
