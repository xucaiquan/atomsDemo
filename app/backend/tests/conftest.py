"""测试地基：SQLite 内存库 + ASGI 客户端 + get_db 覆盖。

为什么用 StaticPool：SQLite 的 ":memory:" 每个连接是一个**独立的库**。
默认连接池会为并发请求开新连接，那些连接看不到已建的表。
StaticPool 让所有会话复用同一个连接，内存库才跨会话可见。

为什么覆盖 get_db 而不是设置 DATABASE_URL：main.py 的 lifespan 会连真实库，
而 ASGITransport 不跑 lifespan；覆盖依赖是唯一能保证测试绝不碰真实库的方式。

退路（若 SQLite 建表失败）：改用
    create_async_engine("sqlite+aiosqlite:///file:testdb?mode=memory&cache=shared&uri=true")
或一个 tmp_path 下的临时文件库。
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# 必须显式导入每个模型模块，Base.metadata 才会被填充。
from models import generation_steps, messages, projects, versions  # noqa: F401
from core.database import Base, get_db
from main import app


@pytest_asyncio.fixture
async def session_maker():
    """函数级内存库：每个测试一个干净的空库。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture
async def client(session_maker):
    """ASGI 测试客户端，get_db 指向内存库。

    ASGITransport 不会执行 lifespan，所以不会触发真实数据库连接。
    """

    async def _override_get_db():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()
