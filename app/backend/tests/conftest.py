"""测试地基（设计文档 2026-09-20 S4）。

- SQLite 内存库 + StaticPool（内存库每连接独立，必须固定单连接）
- httpx.AsyncClient(ASGITransport(app))：不经网络直打 FastAPI
- override get_db + 替换 db_manager 会话工厂：后台生成任务与心跳走
  db_manager.session()（不经过 get_db），只 override 依赖不够
- GENERATION_INLINE=1：受理接口在当前协程内直接跑完流水线，
  测试无需轮询后台任务，避免 flaky（仅测试用开关）

运行方式保持不变：``cd app/backend && python -m pytest tests/ -q``
"""

from __future__ import annotations

import os

# 必须在导入任何应用模块之前设置：
os.environ.setdefault("MGX_IGNORE_MODULE_INIT", "true")
os.environ.setdefault("JWT_SECRET_KEY", "test-jwt-secret")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("JWT_EXPIRE_MINUTES", "60")
os.environ.setdefault("GENERATION_INLINE", "1")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

import models.generation_steps  # noqa: E402,F401 - 注册 ORM 表
import models.messages  # noqa: E402,F401
import models.projects  # noqa: E402,F401
import models.versions  # noqa: E402,F401
from core.database import Base, db_manager, get_db  # noqa: E402


@pytest_asyncio.fixture
async def engine():
    """每个测试一套独立的内存库，测试间零串扰。"""
    eng = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def shared_session_maker(engine):
    """把 db_manager 的会话工厂指向测试库。

    后台生成（_run_generation_in_background）与心跳（_heartbeat）都用
    db_manager.session() 开独立会话；不替换的话它们会去连真实 PostgreSQL。
    """
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    db_manager.engine = engine
    db_manager.async_session_maker = maker
    yield maker
    db_manager.engine = None
    db_manager.async_session_maker = None


@pytest_asyncio.fixture
async def db_session(shared_session_maker):
    async with shared_session_maker() as session:
        yield session


@pytest.fixture
def app(shared_session_maker):
    from main import app as fastapi_app

    async def override_get_db():
        async with shared_session_maker() as session:
            yield session

    fastapi_app.dependency_overrides[get_db] = override_get_db
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture
def http(app):
    """创建独立会话的测试客户端工厂：每个实例 cookiejar 独立，模拟不同访客。"""

    def _make(**kwargs):
        return AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            **kwargs,
        )

    return _make


@pytest_asyncio.fixture
async def client(http):
    async with http() as ac:
        yield ac


@pytest.fixture
def inject_fake_ai():
    """向后台流水线注入 FakeAIHub（S4 可测性改造），测试后自动清理。"""
    import routers.atoms as atoms_module

    def _inject(fake):
        atoms_module._PIPELINE_AI_FACTORY = lambda: fake
        return fake

    yield _inject
    atoms_module._PIPELINE_AI_FACTORY = None


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """重试退避清零，加速失败路径测试（不改生产行为）。"""
    import services.pipeline as pipeline_module

    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
