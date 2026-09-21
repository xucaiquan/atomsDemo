"""冒烟：确认生成的 ORM 模型能在 SQLite 内存库上建表。

若不通过，退路见 conftest.py 顶部注释。
"""

from __future__ import annotations

from sqlalchemy import select

from models.projects import Projects


async def test_sqlite_can_create_tables(shared_session_maker):
    async with shared_session_maker() as session:
        result = await session.execute(select(Projects))
        assert list(result.scalars().all()) == []


async def test_json_roundtrip_with_timezone_column(shared_session_maker):
    """versions.updated_at 是 DateTime(timezone=True)，确认可写可读。

    fixture 名以 ``conftest.py`` 中实际定义的 ``shared_session_maker`` 为准
    （它 yield 会话工厂本身，正是本测试需要的）。
    """
    from datetime import datetime

    from models.versions import Versions

    async with shared_session_maker() as session:
        version = Versions(
            project_public_id="11111111-1111-4111-8111-111111111111",
            seq=1,
            prompt="测试",
            status="pending",
        )
        session.add(version)
        await session.commit()

    async with shared_session_maker() as session:
        result = await session.execute(select(Versions))
        loaded = result.scalars().first()
        assert loaded is not None
        assert loaded.created_at is not None
        assert isinstance(loaded.created_at, datetime)
