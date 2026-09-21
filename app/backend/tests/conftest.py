"""测试地基（设计文档 2026-09-20 S4）。

- SQLite 内存库 + StaticPool（内存库每连接独立，必须固定单连接）
- httpx.AsyncClient(ASGITransport(app))：不经网络直打 FastAPI
- override get_db + 替换 db_manager 会话工厂：后台生成任务走
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
from core.config import settings  # noqa: E402
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

    后台生成（_run_generation_in_background）用 db_manager.session() 开独立
    会话（不经过 get_db）；不替换的话它会去连真实 PostgreSQL。

    yield 出的是**会话工厂本身**，供需要自行开会话的测试使用（如
    test_conftest_smoke.py）。
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
def restore_settings_cache():
    """用例前后快照/恢复 ``settings.__dict__``。

    ``settings.__getattr__`` 读到环境变量后会把值**缓存**进 ``settings.__dict__``
    （见 dependencies/owner.py 的 ``anon_cookie_secure``），而这是**进程级**可变
    状态。用例用 ``monkeypatch.setenv`` + ``delitem(settings.__dict__, ...)`` 覆盖
    它时，teardown 只回滚 ``os.environ``：若键在用例开始时**不存在**，
    ``monkeypatch.delitem(..., raising=False)`` 不登记任何回滚，于是用例内被
    ``__getattr__`` 写回的缓存值会**跨用例残留**。

    已实证的后果：``test_env_override_forces_secure_on_http`` 泄漏
    ``ANON_COOKIE_SECURE='1'``，之后所有用例的 ``anon_cookie_secure()`` 恒为
    True，匿名 cookie 全带 ``Secure``；测试客户端跑在 ``http://testserver`` 上
    不回传 Secure cookie，每个请求都重建匿名身份，表现为「建项目 201、紧接着
    列表为空、generate 404」——28 个用例因此失败，而单独跑每个文件全绿。

    按值恢复而非按键删除：环境变量与缓存的增删改一律不外泄。
    """
    cached = dict(settings.__dict__)
    yield
    settings.__dict__.clear()
    settings.__dict__.update(cached)


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    """重试退避清零，加速失败路径测试（不改生产行为）。

    按生产常量的**实际长度**生成零元组，而不是写死个数——否则
    MAX_ATTEMPTS 调整后这里会 OutOfRange，或静默留下一段未清零的退避。
    """
    import services.pipeline as pipeline_module

    monkeypatch.setattr(
        pipeline_module,
        "RETRY_BACKOFF_SECONDS",
        tuple(0.0 for _ in pipeline_module.RETRY_BACKOFF_SECONDS),
    )
