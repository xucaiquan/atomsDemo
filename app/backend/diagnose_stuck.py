"""排查：找出当前卡在 pending/running 的生成，给出「停在哪一步、停了多久」。

判定一律取库里的时间戳，不猜。关键看三个量：
- versions.updated_at 距今多久（stale 回收看的就是它）
- 哪些 generation_steps 还是 running / pending，各自 started_at 距今多久
- 上一版 HTML 体积（增量时会被整篇回传给模型，是阶段三变慢的主因之一）
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy import select

from core.database import db_manager
from models.generation_steps import Generation_steps
from models.projects import Projects
from models.versions import Versions


def _age(ts) -> str:
    """把 naive 本地时间戳换算成「距今多少秒」。

    注意口径：DB 默认写入的是 naive **本地**时间（此前踩过按 UTC 解释导致
    凭空多算 7 小时的坑），所以这里一律用 datetime.now() 而非 utcnow()。
    """
    if ts is None:
        return "—"
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return f"(无法解析: {ts})"
    if ts.tzinfo is not None:
        ts = ts.replace(tzinfo=None)
    return f"{(datetime.now() - ts).total_seconds():.0f}s 前"


async def main() -> None:
    async with db_manager.session() as db:
        rows = (
            await db.execute(
                select(Versions)
                .where(Versions.status.in_(("pending", "running")))
                .order_by(Versions.updated_at.desc())
            )
        ).scalars().all()

        if not rows:
            print("没有处于 pending/running 的版本（可能已落终态）")

        for v in rows:
            project = (
                await db.execute(
                    select(Projects).where(Projects.public_id == v.project_public_id)
                )
            ).scalars().first()
            print("=" * 72)
            print(f"项目: {project.title if project else '?'}  ({v.project_public_id})")
            print(f"版本 seq={v.seq}  status={v.status}")
            print(f"需求: {(v.prompt or '')[:120]}")
            print(f"created_at={v.created_at} ({_age(v.created_at)})")
            print(f"updated_at={v.updated_at} ({_age(v.updated_at)})")
            print(f"summary={(v.summary or '')[:400]}")

            steps = (
                await db.execute(
                    select(Generation_steps)
                    .where(
                        Generation_steps.project_public_id == v.project_public_id,
                        Generation_steps.version_seq == v.seq,
                    )
                    .order_by(Generation_steps.seq)
                )
            ).scalars().all()
            for s in steps:
                out = (s.output or "")[:80].replace("\n", " ")
                print(
                    f"  步骤{s.seq} {s.name:<8} {s.status:<10} "
                    f"started={s.started_at} ({_age(s.started_at)}) "
                    f"ended={s.ended_at} out={out}"
                )

            # 增量基线体积：阶段三要把它整篇塞进提示词，直接影响上游耗时
            prev = (
                await db.execute(
                    select(Versions)
                    .where(
                        Versions.project_public_id == v.project_public_id,
                        Versions.seq < v.seq,
                        Versions.status == "succeeded",
                    )
                    .order_by(Versions.seq.desc())
                )
            ).scalars().first()
            if prev:
                print(
                    f"  增量基线 v{prev.seq}: HTML {len(prev.html or '')} 字符 "
                    f"(~{len(prev.html or '') // 1024}KB)，阶段三会整篇回传"
                )
            else:
                print("  无成功基线（首轮生成）")


if __name__ == "__main__":
    asyncio.run(main())
