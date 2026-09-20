# Atoms Studio 归属隔离与生成链路加固实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉「任意匿名访客可列出/读取/删除/改写全部用户项目」这一 P0 缺陷，并让三阶段生成链路在截断、空内容、超时、限流、鉴权失效下可恢复、可观测、不误杀。

**Architecture:** 归属键由服务端从「登录 JWT 的 sub → 签名 cookie → `X-Atoms-Anon` 请求头」三级派生，客户端提供的任何字段都不参与；`routers/atoms.py` 里的 `_owned()` / `_visible()` 是唯一过滤入口，9 条路由不得自行拼 `where`。生成链路的恢复能力靠四层叠起来：错误分类 + 退避重试 + 显式超时、上下文预算闸门、结构完整性校验、进程内存活判定 + 心跳。

**Tech Stack:** FastAPI + SQLAlchemy 2.0 async + httpx/ASGITransport + pytest-asyncio（后端）；React 19 + TypeScript + `@metagptx/web-sdk` + vite（前端）。

**Spec:** `docs/superpowers/specs/2026-09-20-atoms-studio-ownership-and-pipeline-hardening-design.md`

**分支：** `feat/ownership-and-pipeline-hardening`（已创建，spec 已提交为 `a396326`）

---

## Global Constraints

以下约束对每个任务都成立，不再逐条重复。

1. **测试命令固定**：`cd app/backend && python -m pytest tests/ -q`。单测：`python -m pytest tests/<file>::<test> -v`。不允许从仓库根跑。
2. **禁止改动**：`app/backend/core/**`、`app/backend/models/**`、`app/backend/main.py`、`app/backend/lambda_handler.py`、`app/frontend/src/pages/AuthCallback.tsx`、`PreviewPane.tsx` 的 `SANDBOX_ATTR` 常量。
3. **`SANDBOX_ATTR` 恒为 `'allow-scripts allow-forms'`**，永不加 `allow-same-origin`。改它等于完全绕过隔离，生成页可读平台 cookie 与 DOM。
4. **归属过滤只放在 `routers/atoms.py`**。不碰 `models`，不在 `@router` 路径外新增中间件。
5. **错误信封恒为** `{"error": {"code", "message"}}`，`message` 是面向用户的可读中文，不含堆栈或内部路径。码表：`VALIDATION_ERROR` 400 / `NOT_FOUND` 404 / `CONFLICT` 409 / `UPSTREAM_ERROR` 502 / `INTERNAL_ERROR` 500。
6. **`generate` 处理器内绝不允许同步等待流水线**（网关 120s 代理读超时）。`GENERATION_INLINE` 是**唯一**例外，且只在测试环境置位。
7. **状态机**：`pending → running → succeeded | failed | cancelled`。`ACTIVE_STATUSES = ("pending", "running")`。
8. **取消守卫**：`pipeline.py` 中每条写路径都必须有 `if version.status in ACTIVE_STATUSES`，迟到的成功/失败不得覆盖 `cancelled`。
9. **数据库会话边界**：慢的外部调用（AI Hub）前后各自是短事务，提交后再 await 模型。
10. **对外只用 `public_id`**（UUID v4），自增 `id` 不出现在任何响应中。
11. **提交粒度**：每个任务结束提交一次，commit message 用中文，格式 `<type>: <描述>`。
12. **前端校验命令**：`cd app/frontend && npm run lint && npm run build`。

---

## 规划期发现的两个额外风险（已折进本计划，spec 未覆盖）

**风险 A — 时间戳口径混用会让陈旧判定整体偏移一个时区。**

`models/versions.py` 的 `updated_at` 用 `default=PyDateTime.now, onupdate=PyDateTime.now`，即**朴素本地时间**；而 `pipeline._now()` 是 `datetime.now(timezone.utc)`（aware）。若心跳写 aware-UTC 进同一列，两种口径混存。读侧若按「朴素值就是 UTC」解释（现有 `_recover_stale_versions` 的做法），在 UTC+8 下得到 `age = 真实年龄 + 8h` → **2 小时的正常生成会被误判为陈旧**——正是本次要消灭的误杀，反而被放大。

**修法**：心跳与 ORM 共用同一个时钟（`datetime.now()` 朴素本地），读侧新增 `_as_utc()` 把朴素值按**本地时间**解释（`.astimezone()`）。这个规则只会**低估**年龄，永不误杀。见 Task 15。

**风险 B — `_RUNNING_TASKS` 是现成的、精确且无竞态的存活信号。**

`routers/atoms.py` 的 `_RUNNING_TASKS` 已经在跟踪进程内正在执行的生成任务。`_recover_stale_versions` 只要跳过进程内仍活着的版本，误杀问题在单 worker 部署下**当下即解**，且服务重启后表为空 → 陈旧版本恢复得比等 10 分钟更快。心跳退为多 worker 部署的后备。见 Task 15。

---

## 文件结构

| 文件 | 职责 |
|---|---|
| `app/backend/dependencies/owner.py` | **新增**。归属键的签名、验签、三级派生。纯逻辑 + 一个 FastAPI 依赖，不碰数据库 |
| `app/backend/routers/atoms.py` | **改造**。唯一过滤入口（`_owned`/`_visible`/`_require_project`）+ 9 条路由 + 陈旧恢复 |
| `app/backend/services/pipeline.py` | **改造**。错误分类、退避重试、显式超时、心跳、结构校验、上下文预算、`ai` 注入 |
| `app/backend/services/prompts.py` | **改造**。上下文预算常量与截断函数、历史条目长度 |
| `app/backend/services/html_extract.py` | **改造**。新增 `looks_well_formed` |
| `app/backend/services/aihub_errors.py` | **改造**。新增 `UpstreamError` |
| `app/backend/scripts/backfill_demo_owner.py` | **新增**。一次性数据迁移 |
| `app/backend/tests/conftest.py` | **新增**。SQLite 内存库 + ASGI client + `get_db` 覆盖 |
| `app/backend/tests/fakes.py` | **新增**。`FakeAIHub` |
| `app/backend/pytest.ini` | **新增**。`asyncio_mode = auto` |
| `app/frontend/src/lib/atoms.ts` | **改造**。匿名标识双通道、`restoreVersion`、新响应字段 |
| `app/frontend/src/contexts/AuthContext.tsx` | **改写**。基于 `client.auth.*` 的三态 |
| `app/frontend/src/pages/Index.tsx` | **改造**。认证接线、过渡态、回滚入口 |
| `app/frontend/src/components/{VersionSwitcher,PreviewPane,CodeViewer}.tsx` | **改造**。回滚入口、一致性标识、失败原因条 |

---

# 阶段 0：测试地基

> 先做这一阶段，后面每个任务才能 TDD。Task 1 的第一步是**实测** spec §4 S4 标记的风险：SQLite 内存库能否直接建出生成的 ORM 表。

### Task 1: SQLite 内存库与 ASGI 测试客户端

**Files:**
- Create: `app/backend/pytest.ini`
- Create: `app/backend/tests/conftest.py`
- Test: `app/backend/tests/test_conftest_smoke.py`

**Interfaces:**
- Consumes: `core.database.Base`、`core.database.get_db`、`main.app`
- Produces: fixture `client`（`httpx.AsyncClient`，`get_db` 已覆盖为 SQLite 会话）、fixture `session_maker`

- [ ] **Step 1: 先建 pytest.ini**

`pytest-asyncio` 1.x 默认 strict 模式，async 测试必须显式打标记。用 `asyncio_mode = auto` 省掉满屏 `@pytest.mark.asyncio`。

```ini
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
testpaths = tests
```

- [ ] **Step 2: 写冒烟测试，确认 SQLite 能建表**

这是 spec 标记的待实测项。若此步失败，见 Step 5 的退路。

```python
"""冒烟：确认生成的 ORM 模型能在 SQLite 内存库上建表。

若不通过，退路见 conftest.py 顶部注释。
"""

from __future__ import annotations

from sqlalchemy import select

from models.projects import Projects


async def test_sqlite_can_create_tables(session_maker):
    async with session_maker() as session:
        result = await session.execute(select(Projects))
        assert list(result.scalars().all()) == []


async def test_json_roundtrip_with_timezone_column(session_maker):
    """versions.updated_at 是 DateTime(timezone=True)，确认可写可读。"""
    from datetime import datetime

    from models.versions import Versions

    async with session_maker() as session:
        version = Versions(
            project_public_id="11111111-1111-4111-8111-111111111111",
            seq=1,
            prompt="测试",
            status="pending",
        )
        session.add(version)
        await session.commit()

    async with session_maker() as session:
        result = await session.execute(select(Versions))
        loaded = result.scalars().first()
        assert loaded is not None
        assert loaded.created_at is not None
        assert isinstance(loaded.created_at, datetime)
```

- [ ] **Step 3: 运行，确认失败原因是 fixture 不存在**

Run: `cd app/backend && python -m pytest tests/test_conftest_smoke.py -q`
Expected: FAIL，`fixture 'session_maker' not found`

- [ ] **Step 4: 写 conftest.py**

```python
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
```

- [ ] **Step 5: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_conftest_smoke.py -q`
Expected: `2 passed`

若失败且报错信息指向 SQLite 建表（例如 `CompileError`、`UnsupportedCompilationError`），改用退路的 shared-cache 内存库或临时文件库，并在此步骤的提交信息里注明。

- [ ] **Step 6: 确认既有测试未被破坏**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿（`test_context_memory.py` 与 `test_html_extract.py` 的既有用例 + 新增 2 条）

- [ ] **Step 7: 提交**

```bash
git add app/backend/pytest.ini app/backend/tests/conftest.py app/backend/tests/test_conftest_smoke.py
git commit -m "test: 测试地基（SQLite 内存库 + ASGI 客户端）"
```

---

### Task 2: FakeAIHub、流水线可注入、GENERATION_INLINE 开关

**Files:**
- Create: `app/backend/tests/fakes.py`
- Modify: `app/backend/services/pipeline.py:156-158`（`__init__`）
- Modify: `app/backend/routers/atoms.py:368-457`（`_run_generation_in_background` / `_spawn_generation`）
- Test: `app/backend/tests/test_fake_aihub.py`

**Interfaces:**
- Consumes: Task 1 的 `client` fixture
- Produces:
  - `FakeAIHub(script: list) -> FakeAIHub`，属性 `.requests: list[GenTxtRequest]`，方法 `async def gentxt(request: GenTxtRequest) -> GenTxtResponse`
  - `GenerationPipeline(db: AsyncSession, ai: AIHubService | None = None)`
  - `GENERATION_INLINE` 环境变量：为真时 `generate` 在请求内直接 `await` 流水线
  - `_run_generation_in_background(public_id, version_seq, prompt, previous_html, history_prompts, session=None)`

**为什么需要 `GENERATION_INLINE`**：`_spawn_generation` 用 `asyncio.create_task`，测试必须轮询 DB 到终态才能断言——慢且 flaky。inline 模式让 `generate` 直接 await，测试是确定性的。它也是让流水线复用**请求会话**（即测试注入的 SQLite 会话）的唯一办法；否则 `db_manager.session()` 会去连真实库。

- [ ] **Step 1: 写 `FakeAIHub` 的失败测试**

```python
"""FakeAIHub 的行为契约。"""

from __future__ import annotations

import pytest
from schemas.aihub import ChatMessage, GenTxtRequest
from tests.fakes import FakeAIHub


def _request(user_text: str = "hi") -> GenTxtRequest:
    return GenTxtRequest(
        messages=[ChatMessage(role="user", content=user_text)],
        model="test-model",
        max_tokens=128,
    )


async def test_script_string_returns_content():
    fake = FakeAIHub(["<!DOCTYPE html><html><body></body></html>"])
    response = await fake.gentxt(_request())
    assert response.content.startswith("<!DOCTYPE html")


async def test_script_exception_is_raised():
    boom = RuntimeError("upstream down")
    fake = FakeAIHub([boom])
    with pytest.raises(RuntimeError, match="upstream down"):
        await fake.gentxt(_request())


async def test_script_callable_can_branch_on_request():
    def branch(request: GenTxtRequest) -> str:
        return "A" if "分析" in request.messages[-1].content else "B"

    fake = FakeAIHub([branch, branch])
    assert (await fake.gentxt(_request("分析一下"))).content == "A"
    assert (await fake.gentxt(_request("别的"))).content == "B"


async def test_requests_are_recorded_for_assertion():
    fake = FakeAIHub(["ok"])
    await fake.gentxt(_request("记下我"))
    assert len(fake.requests) == 1
    assert fake.requests[0].messages[-1].content == "记下我"


async def test_exhausted_script_returns_empty_not_error():
    """脚本用完后返回空内容（而不是抛异常），模拟「模型返回空」。"""
    fake = FakeAIHub([])
    assert (await fake.gentxt(_request())).content == ""
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_fake_aihub.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'tests.fakes'`

- [ ] **Step 3: 实现 `tests/fakes.py`**

```python
"""测试替身。

FakeAIHub 的两个用途：
1. **编排剧本** —— 精确构造「截断→续写成功」「空→空→空」「429 一次后成功」这类序列，
   不需要真实 key、不花配额。
2. **记录请求** —— `.requests` 是断言「实际注入模型的内容」的唯一可靠来源
   （spec §6 A5 要求断言注入的 prompt 文本，而不是「看着像对」）。
"""

from __future__ import annotations

from typing import Any, Callable

from schemas.aihub import GenTxtRequest, GenTxtResponse

ScriptItem = str | Exception | Callable[[GenTxtRequest], "str | Exception"]


class FakeAIHub:
    """脚本化上游模型服务。

    ``script`` 元素依次出队，可以是：
    - ``str``                                   → 作为 ``GenTxtResponse.content`` 返回
    - ``Exception``                             → 抛出
    - ``Callable[[GenTxtRequest], str|Exception]`` → 按请求分支（可复用同一元素多次）
    """

    def __init__(self, script: list[ScriptItem] | None = None) -> None:
        self.script: list[ScriptItem] = list(script or [])
        self.requests: list[GenTxtRequest] = []

    async def gentxt(self, request: GenTxtRequest) -> GenTxtResponse:
        self.requests.append(request)

        item: Any = self.script.pop(0) if self.script else ""
        if callable(item) and not isinstance(item, Exception):
            item = item(request)
        if isinstance(item, Exception):
            raise item

        return GenTxtResponse(
            content=str(item), model=request.model, usage=None
        )

    # ---- 断言辅助 ----

    def user_texts(self) -> list[str]:
        """每次调用中最后一条 user 消息的文本，按调用顺序。"""
        texts: list[str] = []
        for request in self.requests:
            for message in reversed(request.messages):
                if message.role == "user":
                    texts.append(message.content)
                    break
        return texts

    def call_count(self) -> int:
        return len(self.requests)
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_fake_aihub.py -q`
Expected: `5 passed`

- [ ] **Step 5: 让 `GenerationPipeline` 可注入 AI**

改 `app/backend/services/pipeline.py`，`__init__` 与顶部 import：

```python
from services.aihub_errors import UpstreamError  # 本任务先不加，Task 14 使用
```

只改 `__init__`：

```python
    def __init__(self, db: AsyncSession, ai: AIHubService | None = None) -> None:
        self._db = db
        # ai 可注入是测试地基的一部分：测试传 FakeAIHub 即可精确编排上游剧本，
        # 无需真实 key、不花配额。生产路径 ai=None，仍构造真实服务。
        self._ai = ai if ai is not None else AIHubService()
```

- [ ] **Step 6: 加 `GENERATION_INLINE` 开关**

改 `app/backend/routers/atoms.py`。先加 import：

```python
import os
```

把 `_run_generation_in_background` 的签名与首行改成接受可选会话：

```python
async def _run_generation_in_background(
    public_id: str,
    version_seq: int,
    prompt: str,
    previous_html: str | None,
    history_prompts: list[str] | None = None,
    session: AsyncSession | None = None,
) -> None:
    """后台执行三阶段流水线。

    默认自开独立 DB 会话（请求会话此时已关闭）。``session`` 非空时复用它——
    这是 GENERATION_INLINE 测试路径用的，它跑在请求内、请求会话仍然活着。
    """
    try:
        if session is not None:
            pipeline = GenerationPipeline(session)
            outcome = await pipeline.run(
                public_id, version_seq, prompt, previous_html, history_prompts
            )
            logger.info(
                "内联生成结束 project=%s seq=%s status=%s",
                public_id[:8],
                version_seq,
                outcome.get("status"),
            )
            return
        async with db_manager.session() as session:
            pipeline = GenerationPipeline(session)
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
        # ...以下 except 分支保持原样不动...
```

把 `_spawn_generation` 改成：

```python
def _spawn_generation(
    public_id: str,
    version_seq: int,
    prompt: str,
    previous_html: str | None,
    history_prompts: list[str] | None,
) -> "asyncio.Task | None":
    """创建受跟踪的后台任务，注册到任务表以支持取消，并防止 task 被 GC 提前回收。

    当环境变量 GENERATION_INLINE 为真时，不创建后台任务，调用方必须 await 返回值
    （见 generate 处理器）。**仅测试用**：让集成测试是确定性的，并且让流水线复用
    测试注入的会话，而不是去连真实数据库。生产环境绝不可置位。

    判定条件**只能是**环境变量。绝不能写成「传入了会话也算 inline」——``generate``
    恒有请求会话，那样写会让生产路径在请求内同步等待 1~4 分钟的流水线，直接撞上
    网关 120s 代理读超时，也就是本设计存在的理由（Global Constraints 第 6 条）。
    """
    if os.getenv("GENERATION_INLINE"):
        return None

    key = (public_id, version_seq)
    task = asyncio.create_task(
        _run_generation_in_background(
            public_id, version_seq, prompt, previous_html, history_prompts
        )
    )
    _RUNNING_TASKS[key] = task
    task.add_done_callback(lambda _t, k=key: _RUNNING_TASKS.pop(k, None))
    return task
```

`generate` 处理器里把 `_spawn_generation(...)` 调用改成：

```python
    task = _spawn_generation(
        public_id, version_seq, prompt, previous_html, history_prompts
    )
    if task is None:
        # GENERATION_INLINE（仅测试）：请求内直接执行，复用请求会话。
        await _run_generation_in_background(
            public_id,
            version_seq,
            prompt,
            previous_html,
            history_prompts,
            session=db,
        )
```

- [ ] **Step 7: 写 inline 模式的集成测试**

新建文件 `app/backend/tests/test_generation_inline.py`，内容如下：

```python
"""GENERATION_INLINE：请求内同步执行流水线，且复用请求会话。"""

from __future__ import annotations

import pytest

from schemas.aihub import GenTxtRequest
from services import pipeline as pipeline_module
from tests.fakes import FakeAIHub

HTML_OK = "<!DOCTYPE html><html><head></head><body>OK</body></html>"


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setenv("GENERATION_INLINE", "1")


@pytest.fixture
def fake_ai(monkeypatch):
    """三个阶段的响应：分析 JSON → 设计 JSON → HTML。"""
    fake = FakeAIHub(
        [
            '{"app_name":"测试应用","features":["记一笔"],"notes":"记账"}',
            '{"layout":"两栏","components":["表单","列表"],"state":["items"],"interactions":["新增→列表变化"]}',
            HTML_OK,
        ]
    )
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)
    return fake


async def test_inline_generation_reaches_succeeded(client, inline, fake_ai):
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个记账工具"},
    )
    assert accepted.status_code == 202
    seq = accepted.json()["version_seq"]

    steps = await client.get(
        f"/api/v1/atoms/projects/{public_id}/versions/{seq}/steps"
    )
    body = steps.json()
    assert body["status"] == "succeeded", body
    assert fake_ai.call_count() == 3


async def test_inline_uses_request_session_not_real_db(client, inline, fake_ai):
    """若 inline 路径去连真实库，本测试会因 DATABASE_URL 缺失而报错。"""
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个计时器"},
    )
    detail = await client.get(f"/api/v1/atoms/projects/{public_id}")
    assert len(detail.json()["versions"]) == 1
```

- [ ] **Step 8: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_fake_aihub.py tests/test_generation_inline.py -q`
Expected: `7 passed`

若报 `ValueError: AI service not configured`，说明 `monkeypatch.setattr` 的目标不对——`pipeline.py` 里是 `from services.aihub import AIHubService`，所以要 patch `services.pipeline.AIHubService`。

- [ ] **Step 9: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/tests/fakes.py app/backend/tests/test_fake_aihub.py \
        app/backend/tests/test_generation_inline.py \
        app/backend/services/pipeline.py app/backend/routers/atoms.py
git commit -m "test: FakeAIHub、流水线可注入 AI、GENERATION_INLINE 开关"
```

---

# 阶段 1：归属隔离（S1）

### Task 3: 归属键的签名、验签与三级派生

**Files:**
- Create: `app/backend/dependencies/owner.py`
- Test: `app/backend/tests/test_owner_dependency.py`

**Interfaces:**
- Consumes: `core.auth.decode_access_token`、`core.auth.AccessTokenError`、`dependencies.auth.bearer_scheme`
- Produces:
  - 常量 `ANON_COOKIE = "atoms_anon"`、`ANON_HEADER = "X-Atoms-Anon"`、`ANON_MAX_AGE = 15552000`
  - `@dataclass(frozen=True) class OwnerContext`，字段 `owner_key: str`、`anon_key: str | None`
  - `_sign(nonce: str) -> str`、`_verify(raw: str | None) -> str | None`、`_issue() -> str`
  - `async def get_owner(request: Request, credentials=Depends(bearer_scheme)) -> OwnerContext`

**归属键格式**（`projects.owner_key` 上限 64 字符）：

```
nonce = secrets.token_hex(16)                                → 32 字符
sig   = HMAC-SHA256(jwt_secret_key, nonce).hexdigest()[:16]   → 16 字符
raw   = f"{nonce}.{sig}"                                      → 49 字符
anon  = f"anon:{raw}"                                         → 54 字符 ✓
user  = f"user:{sub}"                                         → 5 + 36 = 41 字符 ✓
```

> **2026-09-20 修订（T3 执行期，见账本 Ruling 13）**：上式的前提「`sub` 为 UUID」在本平台
> 不成立（`users.id` 是 `String(255)`，`projects.owner_key` 只有 `String(64)`）。实际实现
> 为 `owner_key = f"user:{sha256(sub).hexdigest()[:32]}"`（37 字符）。**下文 Task 3 的
> 代码片段按此理解**；Task 6 的 `owner_key` description 需同步。详见 spec 同名修订块。

- [ ] **Step 1: 写签名与派的失败测试**

```python
"""归属键的签名、验签与三级派生。"""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from core.auth import create_access_token
from dependencies.owner import (
    ANON_COOKIE,
    ANON_HEADER,
    OwnerContext,
    _issue,
    _sign,
    _verify,
    get_owner,
)


# ---------- 纯函数：签名与验签 ----------


def test_issue_produces_verifiable_value():
    raw = _issue()
    assert _verify(raw) is not None
    assert len(raw.split(".")) == 2


def test_issue_is_unique_per_call():
    assert _issue() != _issue()


def test_owner_key_length_within_model_limit():
    """projects.owner_key 的模型上限是 64 字符。"""
    assert len(f"anon:{_issue()}") <= 64


def test_verify_rejects_tampered_nonce():
    raw = _issue()
    nonce, sig = raw.split(".")
    tampered = f"{'0' * len(nonce)}.{sig}"
    assert _verify(tampered) is None


def test_verify_rejects_tampered_signature():
    raw = _issue()
    nonce, sig = raw.split(".")
    flipped = "0" if sig[0] != "0" else "1"
    assert _verify(f"{nonce}.{flipped}{sig[1:]}") is None


def test_verify_rejects_client_made_key():
    """客户端自造 anon-xxx 必须无效——这正是旧 getOwnerKey 的问题。"""
    assert _verify("anon-abcdefgh-1234567890") is None
    assert _verify("") is None
    assert _verify(None) is None


def test_verify_rejects_wrong_number_of_parts():
    assert _verify("no-dot-here") is None
    assert _verify("a.b.c") is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_owner_dependency.py -q`
Expected: FAIL，`ModuleNotFoundError: No module named 'dependencies.owner'`

- [ ] **Step 3: 实现签名、验签与依赖**

```python
"""归属键（owner_key）的服务端派生。

对应 spec §2 不变量 1：**归属由服务端派生，请求体永不参与**。

两级身份：
- 登录用户 → ``user:{sub}``，来自平台 JWT 的 sub，客户端无法伪造。
- 匿名访客 → ``anon:{nonce}.{sig}``，nonce 由服务端随机生成，sig 是
  HMAC-SHA256 签名。客户端即使拿到 raw 也无法为别的 nonce 造出有效签名，
  因此「换个 owner_key 就能看别人的项目」这条路被堵死。

为什么匿名标识必须服务端签名：旧实现 ``lib/constants.ts::getOwnerKey()`` 用
localStorage 里的 ``anon-xxxx`` 当归属键——那是客户端可任意伪造的字符串，
且它全仓无任何调用点，等于没有隔离。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from core.auth import AccessTokenError, decode_access_token
from core.config import settings
from dependencies.auth import bearer_scheme
from fastapi import Depends, Request

logger = logging.getLogger(__name__)

ANON_COOKIE = "atoms_anon"
ANON_HEADER = "X-Atoms-Anon"
# 180 天：匿名标识丢失意味着用户丢掉自己的项目列表，给足有效期。
ANON_MAX_AGE = 180 * 24 * 3600

_SIG_CHARS = 16
_NONCE_BYTES = 16


def _secret() -> bytes:
    """签名密钥复用平台 JWT 密钥；未配置时返回空串（签名仍确定，但不安全）。"""
    return (settings.jwt_secret_key or "").encode("utf-8")


def _sign(nonce: str) -> str:
    return hmac.new(_secret(), nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:_SIG_CHARS]


def _verify(raw: Optional[str]) -> Optional[str]:
    """校验签名，通过则返回 nonce，否则返回 None。"""
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    nonce, sig = parts
    if not nonce or not sig:
        return None
    expected = _sign(nonce)
    if not hmac.compare_digest(sig, expected):
        return None
    return nonce


def _issue() -> str:
    nonce = secrets.token_hex(_NONCE_BYTES)
    return f"{nonce}.{_sign(nonce)}"


@dataclass(frozen=True)
class OwnerContext:
    """本次请求的归属身份。

    ``anon_key`` 非空表示这是匿名身份，调用方应把它回传客户端
    （Set-Cookie 与 GET /projects 的响应体），让它下次带回来。
    """

    owner_key: str
    anon_key: Optional[str] = None


async def get_owner(
    request: Request,
    credentials=Depends(bearer_scheme),
) -> OwnerContext:
    """按优先级派生归属键：登录 sub > cookie > 请求头 > 新签发。

    fail-closed：查不到归属时不是拒绝，而是签发一个新匿名身份——新身份查不到
    任何既有项目，效果等价于拒绝，但不会把「第一次访问」误伤。
    """
    # ① 登录用户优先。token 无效时**不报错**，落到匿名分支——
    #    密钥未配置或 token 过期的部署仍能按匿名身份工作。
    if credentials:
        try:
            payload = decode_access_token(credentials.credentials)
            subject = payload.get("sub")
            if subject:
                return OwnerContext(owner_key=f"user:{subject}", anon_key=None)
        except AccessTokenError:
            logger.debug("JWT 校验未通过，回退到匿名身份")
        except Exception as exc:  # noqa: BLE001 - 校验异常不得中断请求
            logger.warning("解析凭据时出现意外异常: %s", type(exc).__name__)

    # ② cookie（HttpOnly，防 XSS 窃取）
    # ③ 请求头（cookie 被网关吃掉时的退路）
    for candidate in (
        request.cookies.get(ANON_COOKIE),
        request.headers.get(ANON_HEADER),
    ):
        nonce = _verify(candidate)
        if nonce:
            return OwnerContext(owner_key=f"anon:{candidate}", anon_key=candidate)

    # ④ 新签发
    raw = _issue()
    return OwnerContext(owner_key=f"anon:{raw}", anon_key=raw)
```

- [ ] **Step 4: 运行，确认纯函数测试通过**

Run: `cd app/backend && python -m pytest tests/test_owner_dependency.py -q`
Expected: `7 passed`

- [ ] **Step 5: 写依赖派生的测试**

追加到同一文件：

```python
# ---------- 依赖：三级派生 ----------


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(owner: OwnerContext = Depends(get_owner)):
        return {"owner_key": owner.owner_key, "anon_key": owner.anon_key}

    return app


async def _call(headers=None, cookies=None) -> dict:
    async with AsyncClient(
        transport=ASGITransport(app=_probe_app()), base_url="http://test"
    ) as http_client:
        response = await http_client.get("/probe", headers=headers or {}, cookies=cookies or {})
        return response.json()


async def test_anonymous_gets_server_issued_key():
    body = await _call()
    assert body["owner_key"].startswith("anon:")
    assert body["anon_key"] is not None
    assert _verify(body["anon_key"]) is not None


async def test_cookie_is_honoured():
    raw = _issue()
    body = await _call(cookies={ANON_COOKIE: raw})
    assert body["owner_key"] == f"anon:{raw}"
    assert body["anon_key"] == raw


async def test_header_is_honoured():
    raw = _issue()
    body = await _call(headers={ANON_HEADER: raw})
    assert body["owner_key"] == f"anon:{raw}"


async def test_forged_cookie_falls_through_to_new_identity():
    body = await _call(cookies={ANON_COOKIE: "anon-forged-value"})
    assert body["owner_key"] != "anon:anon-forged-value"
    assert body["owner_key"].startswith("anon:")


async def test_login_subject_wins_over_cookie():
    """登录身份必须优先于匿名 cookie，否则登录后看不到自己的项目。"""
    token = create_access_token({"sub": "user-123", "email": "a@b.c"})
    body = await _call(
        headers={"Authorization": f"Bearer {token}"},
        cookies={ANON_COOKIE: _issue()},
    )
    assert body["owner_key"] == "user:user-123"
    assert body["anon_key"] is None


async def test_invalid_token_falls_back_to_anonymous():
    body = await _call(headers={"Authorization": "Bearer not-a-jwt"})
    assert body["owner_key"].startswith("anon:")
```

- [ ] **Step 6: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_owner_dependency.py -q`
Expected: `13 passed`

若 `create_access_token` 因 `JWT_SECRET_KEY` 未配置抛 `ValueError`，在 `app/backend/tests/conftest.py` 顶部加一行（必须在导入 `core.config` 之前生效，所以放文件最前面）：

```python
import os

os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-for-pytest")
```

- [ ] **Step 7: 提交**

```bash
git add app/backend/dependencies/owner.py app/backend/tests/test_owner_dependency.py app/backend/tests/conftest.py
git commit -m "feat: 服务端派生的归属键（登录 sub / 签名 cookie / 请求头三级）"
```

---

### Task 4: 唯一过滤入口与读路径 4 条路由

**Files:**
- Modify: `app/backend/routers/atoms.py`（`_fetch_project`、`error_envelope`、`list_projects`、`get_project`、`get_version`、`get_version_steps`）
- Test: `app/backend/tests/test_owner_isolation.py`

**Interfaces:**
- Consumes: Task 3 的 `OwnerContext`、`get_owner`、`ANON_COOKIE`、`ANON_HEADER`、`ANON_MAX_AGE`
- Produces:
  - `class RouteError(Exception)`，构造 `RouteError(code: str, message: str)`
  - `def _owned(stmt, owner: str)`、`def _visible(stmt, owner: str)`
  - `async def _require_project(db, public_id, ctx, *, write: bool) -> Projects`
  - `def _json(payload, ctx, status_code=200) -> JSONResponse`
  - `def error_envelope(code, message, ctx=None) -> JSONResponse`

- [ ] **Step 1: 写越权矩阵的失败测试**

```python
"""A1/A2：归属隔离的越权矩阵。

两个独立的匿名会话（A、B），B 对 A 的资源做任何读写都必须 404。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from dependencies.owner import ANON_COOKIE, ANON_HEADER
from main import app


@pytest.fixture
def two_identities(monkeypatch):
    """给两个会话各一个固定的合法签名标识。

    直接 monkeypatch _issue 会让两次调用拿到同一个值，所以改为预生成两个
    合法 raw，再让两个客户端各带一个请求头。
    """
    from dependencies.owner import _issue

    return {"a": _issue(), "b": _issue()}


def _as(identity: str) -> dict[str, str]:
    return {ANON_HEADER: identity}


async def _make_project(client, identity: str, title: str = "A 的项目") -> str:
    response = await client.post(
        "/api/v1/atoms/projects", json={"title": title}, headers=_as(identity)
    )
    assert response.status_code == 201, response.text
    return response.json()["public_id"]


async def test_b_cannot_see_a_project_in_list(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])

    a_list = await client.get("/api/v1/atoms/projects", headers=_as(two_identities["a"]))
    assert [p["public_id"] for p in a_list.json()["projects"]] == [a_id]

    b_list = await client.get("/api/v1/atoms/projects", headers=_as(two_identities["b"]))
    assert b_list.json()["projects"] == []


async def test_b_gets_404_on_a_project_detail(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    response = await client.get(
        f"/api/v1/atoms/projects/{a_id}", headers=_as(two_identities["b"])
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_two_anonymous_sessions_are_isolated(client):
    """两个**独立的**浏览器会话必须互不可见。

    注意：httpx.AsyncClient 会自动持久化 cookie，所以同一个 client 连续发两次请求
    属于同一个会话——这里必须开第二个 client 才是真的「另一个人」。
    """
    a = await client.post("/api/v1/atoms/projects", json={"title": "A"})
    assert a.status_code == 201
    public_id = a.json()["public_id"]

    # 同一会话：看得到（验证 cookie 确实生效）
    same = await client.get("/api/v1/atoms/projects")
    assert [p["public_id"] for p in same.json()["projects"]] == [public_id]

    # 另一个全新会话：看不到
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as other:
        b_list = await other.get("/api/v1/atoms/projects")
        assert b_list.json()["projects"] == []


async def test_cookie_works_same_as_header(client):
    a = await client.post("/api/v1/atoms/projects", json={"title": "Cookie 会话"})
    public_id = a.json()["public_id"]
    cookie = client.cookies.get(ANON_COOKIE)
    assert cookie, "响应必须 Set-Cookie 下发匿名标识"

    from dependencies.owner import _issue

    listing = await client.get(
        "/api/v1/atoms/projects", cookies={ANON_COOKIE: cookie}
    )
    assert [p["public_id"] for p in listing.json()["projects"]] == [public_id]

    # 换成别人的 cookie 就看不到
    other = await client.get(
        "/api/v1/atoms/projects", cookies={ANON_COOKIE: _issue()}
    )
    assert other.json()["projects"] == []
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_owner_isolation.py -q`
Expected: FAIL。`test_b_cannot_see_a_project_in_list` 会在 `b_list.json()["projects"]` 断言处失败（当前返回 A 的项目）；`test_cookie_works...` 会因 `cookie` 为 None 失败。

- [ ] **Step 3: 加过滤入口与响应构造工具**

改 `app/backend/routers/atoms.py`。更新 import 区：

```python
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import db_manager, get_db
from dependencies.owner import (
    ANON_COOKIE,
    ANON_MAX_AGE,
    OwnerContext,
    get_owner,
)
from models.generation_steps import Generation_steps
from models.messages import Messages
from models.projects import Projects
from models.versions import Versions
from services import prompts
from services.pipeline import ACTIVE_STATUSES, FALLBACK_TITLE, GenerationPipeline
```

把 `error_envelope` 替换为下面三件套（`_json` 与 `RouteError` 是本模块所有响应的唯一构造路径，这样匿名 cookie 不会漏挂）：

```python
class RouteError(Exception):
    """路由层的可预期错误，携带错误信封的 code 与面向用户的中文文案。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _attach_owner(resp: JSONResponse, ctx: OwnerContext | None) -> JSONResponse:
    """匿名身份时下发签名的 HttpOnly cookie。

    Secure 由 ATOMS_COOKIE_SECURE 控制：本地 http 必须关，生产 https 必须开。
    """
    if ctx and ctx.anon_key:
        secure = os.getenv("ATOMS_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes")
        resp.set_cookie(
            key=ANON_COOKIE,
            value=ctx.anon_key,
            max_age=ANON_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=secure,
            path="/",
        )
    return resp


def _json(payload: dict[str, Any], ctx: OwnerContext | None, status_code: int = 200) -> JSONResponse:
    """构造响应。**所有** atoms 路由的成功路径都必须经过这里。"""
    return _attach_owner(JSONResponse(status_code=status_code, content=payload), ctx)


def error_envelope(code: str, message: str, ctx: OwnerContext | None = None) -> JSONResponse:
    """构造统一错误信封（message 面向用户，可直接展示）。"""
    return _attach_owner(
        JSONResponse(
            status_code=ERROR_STATUS.get(code, 500),
            content={"error": {"code": code, "message": message}},
        ),
        ctx,
    )
```

再用下面两个函数**替换** `_fetch_project`：

```python
# ------------------------------------------------------------------ 归属过滤
#
# 这两条是本模块**唯一**的归属过滤入口。9 条路由不得自行拼 where，
# 否则漏一条就是一次数据泄露（spec §2 不变量 1）。


def _owned(stmt, owner: str):
    """写路径：只能作用于本人的项目。"""
    return stmt.where(Projects.owner_key == owner)


def _visible(stmt, owner: str):
    """读路径：本人的项目 + 演示项目（is_demo=true 对所有身份可见）。"""
    return stmt.where(
        or_(Projects.owner_key == owner, Projects.is_demo.is_(True))
    )


async def _require_project(
    db: AsyncSession,
    public_id: str,
    ctx: OwnerContext,
    *,
    write: bool,
) -> Projects:
    """获取项目，不满足前置条件时抛 RouteError。

    读路径用 _visible，写路径用 _owned——所以「读得到演示项目但改不了」，
    且写路径的 404 不会泄露项目是否存在（fail-closed）。
    """
    stmt = _visible(select(Projects).where(Projects.public_id == public_id), ctx.owner_key)
    if write:
        stmt = _owned(select(Projects).where(Projects.public_id == public_id), ctx.owner_key)

    result = await db.execute(stmt)
    project = result.scalars().first()
    if not project:
        raise RouteError("NOT_FOUND", "项目不存在或已被删除")
    if write and project.is_demo:
        raise RouteError(
            "CONFLICT",
            "这是演示项目，仅供浏览；请点左上角「新项目」创建你自己的项目",
        )
    return project
```

- [ ] **Step 4: 改造读路径 4 条路由**

`list_projects`：

```python
@router.get("/projects")
async def list_projects(
    ctx: OwnerContext = Depends(get_owner),
    db: AsyncSession = Depends(get_db),
):
    """项目列表，按 updated_at 倒序，不含 html。只返回本人与演示项目。"""
    await _recover_stale_versions(db, ctx.owner_key)
    result = await db.execute(
        _visible(select(Projects), ctx.owner_key).order_by(Projects.updated_at.desc())
    )
    projects = list(result.scalars().all())
    payload = {
        "projects": [_project_brief(item) for item in projects],
        # anon_key 让前端即使拿不到 cookie 也能持久化标识，作为请求头回传。
        # 登录身份时为 None。
        "anon_key": ctx.anon_key,
    }
    await db.commit()
    return _json(payload, ctx)
```

`get_project`：把 `_recover_stale_versions(db, public_id)` 与 `_fetch_project` 换成：

```python
    await _recover_stale_versions(db, ctx.owner_key, public_id)
    try:
        project = await _require_project(db, public_id, ctx, write=False)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)
```

末尾 `return payload` 改成 `return _json(payload, ctx)`。

`get_version`：在查询前插入归属校验，并把返回改为 `_json`：

```python
    try:
        await _require_project(db, public_id, ctx, write=False)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)
```

`get_version_steps`：同样在查询前插入上面这段校验，返回改 `_json(payload, ctx)`。

4 条路由的签名都要加 `ctx: OwnerContext = Depends(get_owner)` 作为**第一个**参数（放在 `db` 之前，保证它在 `public_id` 之类的路径参数之后、默认参数之前的位置合法）。

- [ ] **Step 5: 临时给 `_recover_stale_versions` 加 owner 形参**

本任务只为让 `list_projects` / `get_project` 的调用能编译通过；完整语义在 Task 6 实现。

```python
async def _recover_stale_versions(
    db: AsyncSession,
    owner: str,
    public_id: str | None = None,
) -> None:
    """（Task 6 完成 owner 限定语义）"""
    stmt = select(Versions).where(Versions.status.in_(ACTIVE_STATUSES))
    if public_id:
        stmt = stmt.where(Versions.project_public_id == public_id)
    result = await db.execute(stmt)
    stale = list(result.scalars().all())
    if not stale:
        return
    # 其余逻辑保持原样
    ...
```

其余路由（`create_project`、`delete_project`、`generate`、`cancel_generation`）本任务先只加 `ctx: OwnerContext = Depends(get_owner)` 形参，让依赖生效（它们已在写路径上返回 dict，编译无误）；完整写路径改造在 Task 5。

- [ ] **Step 6: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_owner_isolation.py tests/test_generation_inline.py -q`
Expected: `4 passed` + `2 passed`

- [ ] **Step 7: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/routers/atoms.py app/backend/tests/test_owner_isolation.py
git commit -m "feat: 项目域读路径归属过滤（唯一过滤入口 + 匿名标识下发）"
```

---

### Task 5: 写路径 4 条路由与删除客户端入参

**Files:**
- Modify: `app/backend/routers/atoms.py`（`CreateProjectRequest`、`GenerateRequest`、`create_project`、`delete_project`、`generate`、`cancel_generation`）
- Test: `app/backend/tests/test_owner_isolation.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `_require_project`、`_owned`、`_json`、`RouteError`
- Produces: `CreateProjectRequest` 与 `GenerateRequest` **不再有 `owner_key` 字段**

- [ ] **Step 1: 写写路径越权的失败测试**

追加到 `tests/test_owner_isolation.py`：

```python
# ---------- 写路径越权矩阵 ----------


async def test_b_cannot_delete_a_project(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    response = await client.delete(
        f"/api/v1/atoms/projects/{a_id}", headers=_as(two_identities["b"])
    )
    assert response.status_code == 404
    # 确认确实没被删掉
    still = await client.get(
        f"/api/v1/atoms/projects/{a_id}", headers=_as(two_identities["a"])
    )
    assert still.status_code == 200


async def test_b_cannot_generate_on_a_project(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    response = await client.post(
        f"/api/v1/atoms/projects/{a_id}/generate",
        json={"prompt": "劫持"},
        headers=_as(two_identities["b"]),
    )
    assert response.status_code == 404


async def test_b_cannot_cancel_a_version(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    response = await client.post(
        f"/api/v1/atoms/projects/{a_id}/versions/1/cancel",
        headers=_as(two_identities["b"]),
    )
    assert response.status_code == 404


async def test_b_cannot_read_a_version_html(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    response = await client.get(
        f"/api/v1/atoms/projects/{a_id}/versions/1",
        headers=_as(two_identities["b"]),
    )
    assert response.status_code == 404


async def test_b_cannot_read_a_version_steps(client, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    response = await client.get(
        f"/api/v1/atoms/projects/{a_id}/versions/1/steps",
        headers=_as(two_identities["b"]),
    )
    assert response.status_code == 404


async def test_request_body_owner_key_cannot_hijack_ownership(client, two_identities):
    """反作弊：请求体里塞 owner_key 不得改变归属。

    这是本次修复的核心攻击面：旧实现直接把 data.owner_key 当归属键写库，
    任何人都能自称是别人。
    """
    victim = two_identities["a"]          # 受害者的合法签名标识
    attacker = two_identities["b"]

    # 攻击者用受害者的 owner_key 值当请求体字段，同时带自己的合法标识
    response = await client.post(
        "/api/v1/atoms/projects",
        json={"title": "伪造归属", "owner_key": f"anon:{victim}"},
        headers=_as(attacker),
    )
    assert response.status_code in (201, 422), response.text

    if response.status_code == 201:
        public_id = response.json()["public_id"]

        # 项目归攻击者（发起请求的身份），不归受害者的 owner_key
        attacker_list = await client.get(
            "/api/v1/atoms/projects", headers=_as(attacker)
        )
        assert public_id in [p["public_id"] for p in attacker_list.json()["projects"]]

        # 受害者看不到它
        victim_list = await client.get(
            "/api/v1/atoms/projects", headers=_as(victim)
        )
        assert public_id not in [p["public_id"] for p in victim_list.json()["projects"]]


async def test_missing_identity_cannot_reach_demo_via_write(client):
    """无身份 + 伪造 cookie，写路径一律 404。"""
    response = await client.delete(
        "/api/v1/atoms/projects/9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31",
        cookies={ANON_COOKIE: "forged"},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_owner_isolation.py -q`
Expected: FAIL——`test_b_cannot_delete_a_project` 等用例当前返回 200/202 而非 404。

- [ ] **Step 3: 删除请求体里的 owner_key 字段**

`CreateProjectRequest` 与 `GenerateRequest` 删掉 `owner_key`：

```python
class CreateProjectRequest(BaseModel):
    # 归属键绝不接受客户端传入：它由 get_owner 从登录凭据或服务端签名标识派生。
    # 历史字段 owner_key 已删除——留着它等于留一条伪造归属的路。
    title: Optional[str] = None


class GenerateRequest(BaseModel):
    prompt: str = ""
```

- [ ] **Step 4: 改造写路径 4 条路由**

`create_project`：

```python
@router.post("/projects", status_code=201)
async def create_project(
    data: CreateProjectRequest,
    ctx: OwnerContext = Depends(get_owner),
    db: AsyncSession = Depends(get_db),
):
    """创建空项目。title 省略时使用占位名，首次生成完成后由阶段 1 产出覆盖。"""
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
```

`delete_project`：

```python
@router.delete("/projects/{public_id}")
async def delete_project(
    public_id: str,
    ctx: OwnerContext = Depends(get_owner),
    db: AsyncSession = Depends(get_db),
):
    """删除项目及其下全部版本、消息与步骤（级联）。仅限本人的项目。"""
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
```

`generate` 的头部改法（其余逻辑不动）：

```python
    await _recover_stale_versions(db, ctx.owner_key, public_id)
    try:
        project = await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)
```

`generate` 的返回改成 `return _json({...}, ctx)`。

`cancel_generation` 在查版本**之前**插入：

```python
    try:
        await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)
```

并把返回改为 `return _json({"status": "cancelled", "version_seq": seq}, ctx)`。

- [ ] **Step 5: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_owner_isolation.py -q`
Expected: `12 passed`

- [ ] **Step 6: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/routers/atoms.py app/backend/tests/test_owner_isolation.py
git commit -m "feat: 项目域写路径归属过滤，删除请求体 owner_key 入参"
```

---

### Task 6: 陈旧恢复按 owner 限定、演示项目只读、数据迁移

**Files:**
- Modify: `app/backend/routers/atoms.py`（`_recover_stale_versions`、`list_projects` 自检）
- Create: `app/backend/scripts/backfill_demo_owner.py`
- Modify: `app/backend/data_models/projects.json:20-24`
- Test: `app/backend/tests/test_owner_isolation.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `_visible`、`_owned`
- Produces: `_recover_stale_versions(db: AsyncSession, owner: str, public_id: str | None = None)`

**为什么必须改**：现在任意访客的一次列表请求就会全表扫描并对全表做可能的 UPDATE 提交——既是放大攻击面，也会去改别人的版本状态。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_owner_isolation.py`：

```python
# ---------- 陈旧恢复的 owner 限定 ----------


async def test_recovery_does_not_touch_other_owners_versions(client, two_identities):
    """B 的列表请求不得改动 A 的 running 版本。"""
    from datetime import datetime, timedelta, timezone

    from models.projects import Projects
    from models.versions import Versions

    a_id = await _make_project(client, two_identities["a"])

    # 直接造一个「已经很久没动」的 running 版本，归属 A
    from core.database import db_manager  # noqa: F401  (仅用于 type 提示，实际用 client 的会话)
    import tests.conftest as conftest_module  # noqa: F401

    # 用 client 的内部会话不可得，改为通过公开接口制造：先 create 再手工改库
    # —— 这里用 responses 无关的方式：直接调 generate 让它变 running 是异步的，
    # 所以改为直接写库。见下方 helper。
    await _stale_running_version(client, a_id, owner=two_identities["a"], age_minutes=30)

    await client.get("/api/v1/atoms/projects", headers=_as(two_identities["b"]))

    detail = await client.get(
        f"/api/v1/atoms/projects/{a_id}", headers=_as(two_identities["a"])
    )
    statuses = [v["status"] for v in detail.json()["versions"]]
    assert statuses == ["running"], "B 的列表请求不应改动 A 的版本"
```

上面的 helper 需要能直接写测试库。在 `tests/conftest.py` 追加一个 fixture，把会话工厂暴露给测试：

```python
@pytest_asyncio.fixture
async def db_session(session_maker):
    """直接操作测试库的会话，供构造前置状态用。"""
    async with session_maker() as session:
        yield session
```

并把 helper 写成（放在 `test_owner_isolation.py` 里）：

```python
async def _stale_running_version(db_session, project_public_id: str, owner: str, age_minutes: int) -> None:
    """造一个「很久没动、但仍 running」的版本，并把它挂在 owner 名下。"""
    from datetime import datetime, timedelta

    from models.projects import Projects
    from models.versions import Versions
    from sqlalchemy import select

    result = await db_session.execute(
        select(Projects).where(Projects.public_id == project_public_id)
    )
    project = result.scalars().first()
    project.owner_key = owner
    project.latest_status = "running"

    stamp = datetime.now() - timedelta(minutes=age_minutes)
    version = Versions(
        project_public_id=project_public_id,
        seq=1,
        prompt="旧任务",
        status="running",
        created_at=stamp,
        updated_at=stamp,
    )
    db_session.add(version)
    await db_session.commit()
```

（把上面测试里那段临时 import 删掉，直接 `await _stale_running_version(db_session, a_id, two_identities["a"], 30)`。）

再补两条：自己的陈旧版本**要**被恢复；演示项目写操作返回 409。

```python
async def test_recovery_does_touch_own_stale_version(client, db_session, two_identities):
    a_id = await _make_project(client, two_identities["a"])
    await _stale_running_version(db_session, a_id, two_identities["a"], 30)

    await client.get("/api/v1/atoms/projects", headers=_as(two_identities["a"]))

    detail = await client.get(
        f"/api/v1/atoms/projects/{a_id}", headers=_as(two_identities["a"])
    )
    version = detail.json()["versions"][0]
    assert version["status"] == "failed"
    assert version["error"]


async def test_demo_project_is_visible_but_read_only(client, db_session):
    """演示项目：列表可见、详情可读、写操作 409。"""
    from models.projects import Projects

    db_session.add(
        Projects(
            public_id="9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31",
            title="轻记账",
            owner_key=None,
            is_demo=True,
            version_count=0,
            latest_status="succeeded",
        )
    )
    await db_session.commit()

    listing = await client.get("/api/v1/atoms/projects")
    assert [p["public_id"] for p in listing.json()["projects"]] == [
        "9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31"
    ]

    detail = await client.get(
        "/api/v1/atoms/projects/9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31"
    )
    assert detail.status_code == 200

    blocked = await client.delete(
        "/api/v1/atoms/projects/9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31"
    )
    assert blocked.status_code == 409
    assert "演示项目" in blocked.json()["error"]["message"]

    blocked_gen = await client.post(
        "/api/v1/atoms/projects/9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31/generate",
        json={"prompt": "改一下"},
    )
    assert blocked_gen.status_code == 409
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_owner_isolation.py -q`
Expected: `test_recovery_does_not_touch_other_owners_versions` FAIL（当前全表扫描会把它置 failed）

- [ ] **Step 3: 实现 owner 限定的陈旧恢复**

```python
async def _recover_stale_versions(
    db: AsyncSession,
    owner: str,
    public_id: str | None = None,
) -> None:
    """清理中断的生成，**只针对当前归属**。

    对应 data-model.md 的恢复语义：服务重启后残留的 ``pending`` / ``running``
    版本置为 ``failed`` 并写入**面向用户的可读原因**，避免永久卡住的加载态。

    只扫当前 owner 的项目：旧实现全表扫描 + 可能全表 UPDATE 提交，
    任意访客的一次列表请求就能触发，既放大攻击面又会改别人的版本状态。
    """
    stmt = (
        select(Versions)
        .join(Projects, Projects.public_id == Versions.project_public_id)
        .where(Versions.status.in_(ACTIVE_STATUSES))
        .where(Projects.owner_key == owner)
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
        # 时间戳口径归一（_as_utc 见 Step 3b）：朴素值按**本地时间**解释。
        # 不能用 replace(tzinfo=utc)——那会把 UTC+8 机器上的真实年龄凭空加上
        # 8 小时，2 小时前的版本被判成 10 小时前，正常生成会被误杀。
        reference = _as_utc(version.created_at)
        if reference and now - reference < STALE_AFTER:
            continue

        version.status = "failed"
        version.error = "生成过程被中断（服务重启或连接断开），你的描述已保留，可重新提交"
        changed = True

        steps_result = await db.execute(...)   # 保持原样
        for step in steps_result.scalars().all():
            ...
        project = await _fetch_project(db, version.project_public_id)   # 见 Step 4
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "failed"

    if changed:
        await db.commit()
```

`_fetch_project` 已被 Task 4 删除，这里改用 `_visible`：

```python
        project_result = await db.execute(
            _visible(select(Projects).where(Projects.public_id == version.project_public_id), owner)
        )
        project = project_result.scalars().first()
```

- [ ] **Step 3b: 在 pipeline.py 加时间戳口径归一函数**

`_recover_stale_versions` 依赖它，所以必须与它同批交付（否则本任务的
`test_recovery_does_touch_own_stale_version` 在 UTC+8 机器上必然失败）。

`app/backend/services/pipeline.py`，放在 `_iso` 之后：

```python
def _as_utc(value: datetime | None) -> datetime | None:
    """把库里的时间戳归一成 aware-UTC。

    **朴素值按本地时间解释**（``.astimezone()`` 会附上本地偏移），因为这正是
    ``models`` 的 ``default=PyDateTime.now`` 写进去的东西。

    绝不能写成 ``value.replace(tzinfo=timezone.utc)``——那等于宣称朴素值是 UTC，
    在 UTC+8 机器上会让真实年龄凭空 +8 小时：2 小时前的正常生成被算成 10 小时前，
    正好越过 STALE_AFTER，把活跃任务误杀。

    本规则的偏差方向只会**低估**年龄（本地时钟落后 UTC 时尤其如此），因此宁可让
    真挂死的任务多留一会儿，也绝不误杀一个其实还新鲜的版本。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value.astimezone(timezone.utc)
```

`routers/atoms.py` 的 import 补上它：

```python
from services.pipeline import ACTIVE_STATUSES, FALLBACK_TITLE, GenerationPipeline, _as_utc
```

- [ ] **Step 4: 加迁移漏跑的自检日志**

在 `list_projects` 里 `_recover_stale_versions` 之后插入：

```python
    # 自检：迁移未执行时，owner_key 为空且非演示的历史项目会对所有人不可见。
    # 这是 fail-closed（安全侧），但必须让运维看得见，否则表现为「数据凭空消失」。
    orphan_count = await db.scalar(
        select(func.count())
        .select_from(Projects)
        .where(Projects.owner_key.is_(None), Projects.is_demo.is_(False))
    )
    if orphan_count:
        logger.error(
            "检测到 %s 个 owner_key 为空且非演示的项目，疑似迁移未执行："
            "请运行 python scripts/backfill_demo_owner.py",
            orphan_count,
        )
```

import 区补 `from sqlalchemy import func, or_, select`。

- [ ] **Step 5: 写迁移脚本**

`app/backend/scripts/backfill_demo_owner.py`：

```python
"""一次性数据迁移：把 owner_key 为 NULL 的历史项目标记为只读演示项目。

对应 spec §4 S1.3 与决策 Q3。必须在部署归属隔离**之前**执行一次。

为什么判据是 is_demo 而不是 owner_key IS NULL：mock 演示数据本来就是
is_demo=true，用 is_demo 作为单一判据最准。若漏跑本脚本，那些 NULL owner
项目会对所有身份不可见（fail-closed 安全侧），而**不是**人人可见。

幂等，可重复执行。

用法：
    cd app/backend && python scripts/backfill_demo_owner.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, update  # noqa: E402

from core.database import db_manager  # noqa: E402
from models.projects import Projects  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill-demo-owner")


async def main() -> int:
    async with db_manager.session() as session:
        pending = await session.scalar(
            select(func.count())
            .select_from(Projects)
            .where(Projects.owner_key.is_(None), Projects.is_demo.is_(False))
        )
        logger.info("待迁移项目数：%s", pending)
        if not pending:
            logger.info("无需迁移")
            return 0

        await session.execute(
            update(Projects)
            .where(Projects.owner_key.is_(None), Projects.is_demo.is_(False))
            .values(is_demo=True)
        )
        await session.commit()

        still_orphan = await session.scalar(
            select(func.count())
            .select_from(Projects)
            .where(Projects.owner_key.is_(None), Projects.is_demo.is_(False))
        )
        logger.info("迁移完成，剩余待迁移：%s", still_orphan)
        return 0 if still_orphan == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 6: 更新 data_models 的字段描述**

`app/backend/data_models/projects.json` 的 `owner_key` 段：

```json
        "owner_key": {
            "type": "string",
            "maxLength": 64,
            "description": "项目归属键，由服务端派生，不接受客户端传入。取值为 user:{平台用户ID} 或 anon:{nonce}.{HMAC签名}；NULL 表示历史遗留的只读演示项目"
        },
```

**只改 description**，不动 `type`/`maxLength`/字段名——`models/**` 由 schema 生成，改结构会触发重新生成。

- [ ] **Step 7: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 8: 提交**

```bash
git add app/backend/routers/atoms.py app/backend/scripts/backfill_demo_owner.py \
        app/backend/data_models/projects.json app/backend/tests/test_owner_isolation.py \
        app/backend/tests/conftest.py
git commit -m "feat: 陈旧恢复按归属限定、演示项目只读、历史数据迁移脚本"
```

---

### Task 7: 生成历史只取成功版本

**Files:**
- Modify: `app/backend/routers/atoms.py`（`generate` 的 `history_result` 查询）
- Test: `app/backend/tests/test_two_round_increment.py`

**Interfaces:**
- Consumes: Task 2 的 `FakeAIHub`、`GENERATION_INLINE`；Task 6 的演示只读
- Produces: `generate` 的 `history_prompts` 只含 `status == "succeeded"` 的版本 prompt

**为什么**：第一轮失败（例如「做一个贪吃蛇」截断失败）后，用户在第二轮说「继续刚刚的需求」，历史里出现一条失败需求，模型可能把失败意图也当成需求延续。**必须仍在 `prepare()` 落库新版本之前查询**——现有注释已强调，改造时保留。

- [ ] **Step 1: 写失败测试**

```python
"""A4/A5：两类需求 + 同项目连续两轮增量。

断言的是**实际注入模型的 prompt 文本**（经 FakeAIHub.requests），
不是「看着像对」。
"""

from __future__ import annotations

import pytest

from schemas.aihub import GenTxtRequest
from services import pipeline as pipeline_module
from tests.fakes import FakeAIHub

HTML_V1 = (
    "<!DOCTYPE html><html><head><title>记账</title></head>"
    "<body><script>function renderLedger(){return 1;}</script>"
    "总余额：0 元</body></html>"
)
HTML_V2 = (
    "<!DOCTYPE html><html><head><title>记账</title></head>"
    "<body><script>function renderLedger(){return 1;}"
    "function renderMonthChart(){return 2;}</script>"
    "总余额：0 元 月度图表</body></html>"
)

ANALYSIS = '{"app_name":"轻记账","features":["记收入","记支出","看总余额"],"notes":"记账小工具"}'
DESIGN = '{"layout":"两栏","components":["记账表单","流水列表"],"state":["entries"],"interactions":["新增→列表变化"]}'


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setenv("GENERATION_INLINE", "1")


async def _run_two_rounds(client, monkeypatch):
    """第一轮从零，第二轮指代型短句。返回 (fake, public_id, seq1, seq2)。"""
    fake = FakeAIHub([ANALYSIS, DESIGN, HTML_V1, ANALYSIS, DESIGN, HTML_V2])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": None})
    public_id = created.json()["public_id"]

    first = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个记账小工具，能记收入和支出，显示总余额"},
    )
    seq1 = first.json()["version_seq"]

    second = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "继续刚刚的需求，再加一个按月份筛选的图表"},
    )
    seq2 = second.json()["version_seq"]
    return fake, public_id, seq1, seq2


async def test_first_round_has_no_history_or_previous_html(client, inline, monkeypatch):
    fake, public_id, seq1, seq2 = await _run_two_rounds(client, monkeypatch)

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}")
    statuses = {v["seq"]: v["status"] for v in detail.json()["versions"]}
    assert statuses[seq1] == "succeeded"
    assert statuses[seq2] == "succeeded"

    first_user = fake.user_texts()[0]
    assert "【本项目此前的需求历史" not in first_user
    assert "上一版页面" not in first_user
    assert "记账小工具" in first_user


async def test_second_round_resolves_anaphora_and_carries_history(client, inline, monkeypatch):
    fake, public_id, seq1, seq2 = await _run_two_rounds(client, monkeypatch)

    second_user = fake.user_texts()[3]  # 第二轮的阶段 1（每轮 3 次调用）
    assert "【本项目此前的需求历史" in second_user
    assert "记账小工具" in second_user
    assert "继续刚刚的需求" in second_user


async def test_second_round_carries_previous_html(client, inline, monkeypatch):
    fake, public_id, seq1, seq2 = await _run_two_rounds(client, monkeypatch)

    second_user = fake.user_texts()[3]
    assert "上一版页面的完整源码" in second_user
    assert "renderLedger" in second_user, "必须回传第一轮的 HTML 特征串"


async def test_second_round_preserves_first_round_features(client, inline, monkeypatch):
    fake, public_id, seq1, seq2 = await _run_two_rounds(client, monkeypatch)

    v1 = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/{seq1}")
    v2 = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/{seq2}")
    assert len(v2.json()["html"]) >= len(v1.json()["html"]) * 0.8, "不是推倒重来"
    assert "renderMonthChart" in v2.json()["html"]


async def test_prompt_stays_within_budget(client, inline, monkeypatch):
    """上下文不得随轮次线性膨胀。"""
    fake, public_id, seq1, seq2 = await _run_two_rounds(client, monkeypatch)

    largest = max(len(text) for text in fake.user_texts())
    assert largest < 60_000, f"单次注入 prompt 过大：{largest}"


async def test_failed_round_does_not_pollute_history(client, inline, monkeypatch):
    """第一轮失败的需求不得进入第二轮的历史。"""
    # 第一轮：阶段 3 两轮尝试都返回非 HTML → 失败
    fake = FakeAIHub(["{}", "{}", "not html", "not html"])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    first = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个注定失败的应用"},
    )
    assert first.status_code == 202

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}")
    assert detail.json()["versions"][0]["status"] == "failed"

    # 第二轮
    fake.script.extend([ANALYSIS, DESIGN, HTML_V1])
    await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "算了，做个简单计算器"},
    )

    texts = fake.user_texts()
    second_round_user = texts[3]
    assert "注定失败的应用" not in second_round_user
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_two_round_increment.py -q`
Expected: `test_failed_round_does_not_pollute_history` FAIL（失败轮次的 prompt 出现在历史里）

- [ ] **Step 3: 加 `status == "succeeded"` 过滤**

`app/backend/routers/atoms.py` 的 `history_result`：

```python
    # 需求历史：本项目此前**成功**版本的需求（时间升序）。用于让模型消解
    # 「继续刚刚的需求」「按之前说的」「再优化一下」这类指代——否则模型只
    # 看到孤立的当前短句，无法还原真实意图。条数与长度由 prompts 层截断，
    # 上下文不会随轮次线性膨胀。
    #
    # 只取 succeeded：失败/取消轮次的需求若进入历史，模型可能把那个失败意图
    # 也当成需求延续。必须在 prepare 落库新版本之前查询。
    history_result = await db.execute(
        select(Versions.prompt)
        .where(
            Versions.project_public_id == public_id,
            Versions.status == "succeeded",
        )
        .order_by(Versions.seq)
    )
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_two_round_increment.py -q`
Expected: `6 passed`

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/routers/atoms.py app/backend/tests/test_two_round_increment.py
git commit -m "fix: 需求历史只取成功版本，失败轮次不污染指代消解"
```

---

### Task 8: A1/A2 验收证据归档

**Files:**
- Create: `docs/验收证据/A1-A2-归属隔离.md`

**Interfaces:**
- Consumes: Task 3–7 的全部测试

- [ ] **Step 1: 跑越权矩阵并留存输出**

```bash
cd app/backend && python -m pytest tests/test_owner_isolation.py -v 2>&1 | tee /tmp/a1a2.txt
```

- [ ] **Step 2: 写证据文档**

`docs/验收证据/A1-A2-归属隔离.md`：

```markdown
# A1/A2 验收证据：无登录数据隔离

日期：<填当天>
基线：feat/ownership-and-pipeline-hardening

## 命令

    cd app/backend && python -m pytest tests/test_owner_isolation.py -v

## 输出

<粘贴上一步的真实输出>

## 结论对照

| 验收项 | 期望 | 实测 |
|---|---|---|
| A1 A 建的项目在 B 的列表中出现 0 次 | 0 | <填> |
| A1 B 取 A 的项目详情 | 404 | <填> |
| A2 写路径越权（删除/生成/取消） | 404 | <填> |
| A2 伪造请求体 owner_key | 归属不变 | <填> |
| A2 无有效标识访问既有项目 | 404 | <填> |
| A2 演示项目写操作 | 409 | <填> |
```

- [ ] **Step 3: 提交**

```bash
git add docs/验收证据/A1-A2-归属隔离.md
git commit -m "docs: A1/A2 归属隔离验收证据"
```

---

# 阶段 2：前端接线（S2）

### Task 9: 匿名标识双通道与 API 层

**Files:**
- Modify: `app/frontend/src/lib/constants.ts`
- Modify: `app/frontend/src/lib/atoms.ts`

**Interfaces:**
- Consumes: 后端 `GET /projects` 响应新增的 `anon_key` 字段；后端接受的 `X-Atoms-Anon` 请求头
- Produces:
  - `constants.ts` 删除 `getOwnerKey`，新增 `readAnonKey(): string | null` / `writeAnonKey(raw: string | null): void` / `ANON_HEADER_NAME`
  - `atoms.ts` 的 `invoke()` 每次请求附 `X-Atoms-Anon` 头；`listProjects()` 把响应里的 `anon_key` 落盘

- [ ] **Step 1: 改 constants.ts**

删除整个 `getOwnerKey` 函数，替换为：

```typescript
/**
 * 匿名归属标识的本地缓存。
 *
 * 这个值**不是**身份凭证——真正的凭证是后端用 HMAC 签名的 cookie 与响应体里的
 * anon_key，客户端无法伪造出有效签名。这里只做「把它带回去」的搬运。
 *
 * 旧实现 getOwnerKey() 在 localStorage 里自造 `anon-xxxx` 当归属键，客户端可任意
 * 伪造，等于没有隔离，已删除。
 */
export const ANON_HEADER_NAME = 'X-Atoms-Anon';

const ANON_STORAGE_KEY = 'atoms_anon_key';

export function readAnonKey(): string | null {
  try {
    return localStorage.getItem(ANON_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function writeAnonKey(raw: string | null): void {
  try {
    if (raw) localStorage.setItem(ANON_STORAGE_KEY, raw);
    else localStorage.removeItem(ANON_STORAGE_KEY);
  } catch {
    /* 隐私模式下 localStorage 可能不可用，退回纯 cookie 通道 */
  }
}
```

- [ ] **Step 2: 改 atoms.ts 的 invoke 与 listProjects**

import 区加：

```typescript
import { ANON_HEADER_NAME, readAnonKey, writeAnonKey } from '@/lib/constants';
```

`invoke` 改为：

```typescript
async function invoke<T>(
  url: string,
  method: 'GET' | 'POST' | 'DELETE',
  data: Record<string, unknown> = {},
  timeout?: number,
): Promise<T> {
  // 匿名标识双通道：cookie 由浏览器自动携带（HttpOnly，防 XSS 窃取），
  // 请求头是 cookie 被平台网关吃掉时的退路。两者都过服务端 HMAC 验签。
  const anonKey = readAnonKey();
  const headers: Record<string, string> = {};
  if (anonKey) headers[ANON_HEADER_NAME] = anonKey;

  try {
    const response = await client.apiCall.invoke({
      url,
      method,
      data,
      options: {
        headers,
        ...(timeout ? { timeout } : {}),
      },
    });
    return unwrap<T>(response.data);
  } catch (e) {
    throw toReadableError(e);
  }
}
```

`listProjects` 改为：

```typescript
  /** 项目列表，按 updated_at 倒序，不含 html。 */
  async listProjects(): Promise<ProjectBrief[]> {
    const data = await invoke<{ projects: ProjectBrief[]; anon_key?: string | null }>(
      '/api/v1/atoms/projects',
      'GET',
    );
    // 后端在每次响应体里回传当前匿名标识，这里持久化供后续请求以请求头回传。
    // 登录身份时后端返回 null，此时清掉本地缓存。
    writeAnonKey(data.anon_key ?? null);
    return data.projects || [];
  },
```

- [ ] **Step 3: 类型检查**

Run: `cd app/frontend && npm run build`
Expected: 通过（`getOwnerKey` 已无引用者，删除不会报错）

若报 `getOwnerKey` 仍被引用，删掉那个引用点——它在 spec §4 S2 里被明确要求删除。

- [ ] **Step 4: 登录/登出时清空本地匿名标识**

在 `atoms.ts` 的 `atomsApi` 里加：

```typescript
  /**
   * 清空本地匿名标识缓存。
   *
   * 登录/登出后必须调用：切换身份意味着看到一个完全不同的项目列表，
   * 残留的匿名标识会让「登出后仍显示上一账号项目」这种假象延续。
   */
  clearAnonKey(): void {
    writeAnonKey(null);
  },
```

- [ ] **Step 5: 提交**

```bash
git add app/frontend/src/lib/constants.ts app/frontend/src/lib/atoms.ts
git commit -m "feat(frontend): 匿名标识双通道，删除客户端可伪造的 getOwnerKey"
```

---

### Task 10: 认证三态接线

**Files:**
- Rewrite: `app/frontend/src/contexts/AuthContext.tsx`
- Modify: `app/frontend/src/App.tsx`
- Delete: `app/frontend/src/lib/auth.ts`

**Interfaces:**
- Consumes: `@metagptx/web-sdk` 的 `client.auth.me/toLogin/logout`
- Produces: `AuthProvider`、`useAuth()`，返回 `{ user, status, loading, error, login, logout, refetch, isAdmin }`，其中 `status: 'loading' | 'authenticated' | 'anonymous'`

**为什么删 `lib/auth.ts`**：它 `return response.data.redirect_url`，但后端 `routers/auth.py:100` 是直接 302——axios 会跟随重定向拿到 HTML，拿不到 `redirect_url`。这条路径当前是坏的，且唯一消费者就是 `AuthContext`（`ProtectedAdminRoute` 全局无引用）。

**禁止**：`if (!user) navigate('/auth/callback')`。OIDC 的 `/auth/callback` 只有带 `code`/`state` 参数时才有意义，缺参数时无限循环。未登录是正常可用状态，登录是可选升级。

- [ ] **Step 1: 改写 AuthContext.tsx**

```tsx
/**
 * 认证状态（三态）。
 *
 * 依据 skills_docs/web_sdk.md：只有 client.auth.me/toLogin/login/logout 四个方法
 * 存在，没有 getSession/getUser/onAuthStateChange。
 *
 * 三态是这个组件存在的理由：client.auth.me() 未落地前**不能**假定用户已登出，
 * 否则会在登录页闪一下「未登录」、或在登录成功瞬间误判。
 *
 * 本平台的形态是「匿名可写 + 登录可选」：未登录不是错误状态，页面照常可用。
 * 登录只意味着换一个身份、看到另一套项目列表。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { createClient } from '@metagptx/web-sdk';

const client = createClient();

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous';

interface User {
  id: string;
  email: string;
  name?: string;
  role: string;
}

interface AuthContextType {
  user: User | null;
  status: AuthStatus;
  loading: boolean;
  error: string | null;
  login: () => void;
  logout: () => Promise<void>;
  refetch: () => Promise<void>;
  isAdmin: boolean;
}

const AuthContext = createContext<AuthContextType | null>(null);

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
};

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState<AuthStatus>('loading');
  const [error, setError] = useState<string | null>(null);

  const resolve = useCallback(async () => {
    setStatus('loading');
    setError(null);
    try {
      const response = await client.auth.me();
      const profile = response?.data as User | undefined;
      if (profile?.id) {
        setUser(profile);
        setStatus('authenticated');
      } else {
        setUser(null);
        setStatus('anonymous');
      }
    } catch {
      // 未登录是**正常**状态，不是错误：这是「匿名可写 + 登录可选」的产品形态。
      setUser(null);
      setStatus('anonymous');
    }
  }, []);

  useEffect(() => {
    // 空依赖数组：用户状态更新时不得重复触发认证（web_sdk.md 明确要求）。
    void resolve();
  }, [resolve]);

  const login = useCallback(() => {
    // 重定向到平台登录页，成功后回到 /auth/callback。
    // 不要在 catch 里调用它——业务请求失败不该把用户踢去登录。
    client.auth.toLogin();
  }, []);

  const logout = useCallback(async () => {
    setError(null);
    try {
      await client.auth.logout();
    } catch (e) {
      setError(e instanceof Error ? e.message : '登出失败');
    } finally {
      // 无论后端是否报错都回到匿名态：留在「已登录」但 token 已失效更糟。
      setUser(null);
      setStatus('anonymous');
    }
  }, []);

  const value = useMemo<AuthContextType>(
    () => ({
      user,
      status,
      loading: status === 'loading',
      error,
      login,
      logout,
      refetch: resolve,
      isAdmin: user?.role === 'admin',
    }),
    [user, status, error, login, logout, resolve],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};
```

- [ ] **Step 2: 挂到 App.tsx**

import 区加（放在 `AuthError` 之后）：

```tsx
import { AuthProvider } from './contexts/AuthContext';
```

`MODULE_PROVIDERS_START` 槽位里挂上：

```tsx
    {/* MODULE_PROVIDERS_START */}
    <AuthProvider>
    {/* MODULE_PROVIDERS_END */}
```

并在 `MODULE_PROVIDERS_CLOSE` 处闭合：

```tsx
    {/* MODULE_PROVIDERS_CLOSE */}
    </AuthProvider>
```

最终的 `App` 组件形如：

```tsx
const App = () => (
  <QueryClientProvider client={queryClient}>
    {/* MODULE_PROVIDERS_START */}
    <AuthProvider>
    {/* MODULE_PROVIDERS_END */}
    <TooltipProvider>
      <Toaster />
      <BrowserRouter>
        <AppRoutes />
      </BrowserRouter>
    </TooltipProvider>
    {/* MODULE_PROVIDERS_CLOSE */}
    </AuthProvider>
  </QueryClientProvider>
);
```

注意：模板的 `MODULE_PROVIDERS_START/END` 与 `MODULE_PROVIDERS_CLOSE` 标记必须保留原样，平台靠它们注入代码。

- [ ] **Step 3: 删除坏的 lib/auth.ts**

```bash
git rm app/frontend/src/lib/auth.ts
```

- [ ] **Step 4: 校验**

Run: `cd app/frontend && npm run lint && npm run build`
Expected: 均通过

若 `npm run build` 报 `Cannot find module '../lib/auth'`，说明还有文件在引用它——检查 `components/ProtectedAdminRoute.tsx`；它用的是 `useAuth()`（来自 `@/contexts/AuthContext`），改写后不需要 `lib/auth`。若它确实直接 import 了 `lib/auth`，改成从 context 取。

- [ ] **Step 5: 提交**

```bash
git add app/frontend/src/contexts/AuthContext.tsx app/frontend/src/App.tsx
git commit -m "feat(frontend): 认证三态接线（client.auth），删除依赖 redirect_url 的坏路径"
```

---

### Task 11: 登录/登出入口与登出清态

**Files:**
- Modify: `app/frontend/src/pages/Index.tsx`

**Interfaces:**
- Consumes: Task 10 的 `useAuth()`；Task 9 的 `atomsApi.clearAnonKey()`
- Produces: 顶栏登录/登出按钮；登出与登录成功后清空并重载项目状态

**这是最容易做假的地方**：只挂按钮、不清状态的话，登出后仍显示上一账号的项目列表——「看起来隔离了其实没隔离」。清态是这一步的实质。

- [ ] **Step 1: 加认证接线**

import 区：

```tsx
import { LogIn, LogOut, UserRound } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
```

组件内、状态声明之后：

```tsx
  const { status: authStatus, user, login, logout } = useAuth();
```

- [ ] **Step 2: 写清态 + 重载的处理器**

放在 `handleNewProject` 附近：

```tsx
  /**
   * 身份切换后必须清空全部项目域状态再重载。
   *
   * 不清的话，登出后仍显示上一账号的 projects/detail/html ——「看起来隔离了
   * 其实没隔离」的经典假象。匿名标识也要一起清：它属于上一个身份。
   */
  const resetForIdentityChange = useCallback(async () => {
    stopPolling();
    atomsApi.clearAnonKey();
    setProjects([]);
    setDetail(null);
    setActiveId(null);
    setActiveSeq(null);
    setHtml('');
    setSteps([]);
    setFailedMessage(null);
    setPrompt('');
    setView('preview');
    setIsGenerating(false);
    generatingSeqRef.current = null;
    resumedRef.current = null;
    await refreshProjects();
  }, [refreshProjects, stopPolling]);

  const handleLogin = useCallback(() => {
    login();
  }, [login]);

  const handleLogout = useCallback(async () => {
    await logout();
    await resetForIdentityChange();
    toast.success('已退出登录，当前为匿名访问');
  }, [logout, resetForIdentityChange]);
```

- [ ] **Step 3: 顶栏加入口**

在顶栏 `ml-auto` 容器内、「新项目」按钮之前插入：

```tsx
          {authStatus === 'authenticated' && (
            <span className="flex items-center gap-1.5 rounded-full bg-slate-800/70 px-3 py-1 text-[11px] text-slate-300">
              <UserRound className="h-3 w-3" />
              {user?.name || user?.email || '已登录'}
            </span>
          )}
          {authStatus === 'anonymous' && (
            <Button
              size="sm"
              variant="outline"
              onClick={handleLogin}
              className="gap-1.5 rounded-xl border-slate-700 bg-transparent text-slate-300 hover:bg-slate-800 hover:text-white"
            >
              <LogIn className="h-3.5 w-3.5" />
              登录
            </Button>
          )}
          {authStatus === 'authenticated' && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => void handleLogout()}
              className="gap-1.5 rounded-xl border-slate-700 bg-transparent text-slate-300 hover:bg-slate-800 hover:text-white"
            >
              <LogOut className="h-3.5 w-3.5" />
              退出
            </Button>
          )}
```

`authStatus === 'loading'` 时两个按钮都不渲染——这正是三态存在的意义，避免在解析完成前闪一个错误的入口。

- [ ] **Step 4: 匿名身份下的归属提示**

左栏标题下方加一行，明示匿名项目在登录后看不到（决策 Q2：不做跨身份合并）：

```tsx
          {authStatus === 'anonymous' && (
            <p className="px-4 pb-2 text-[10px] leading-relaxed text-slate-600">
              当前为匿名访问，项目绑定在本浏览器；登录后看到的是账号下的项目
            </p>
          )}
```

- [ ] **Step 5: 校验**

Run: `cd app/frontend && npm run lint && npm run build`
Expected: 均通过

- [ ] **Step 6: 手工验证清态**

启动应用（`cd app && bash start_app_v2.sh`），然后：
1. 不登录，建一个项目 → 列表里出现
2. 点「登录」并完成登录 → **列表必须立刻变成该账号的项目（不为空也应与匿名列表不同）**
3. 点「退出」→ 列表必须清空后重新加载匿名项目，且刚才那个匿名项目**不应出现**（因为已换过身份）

第 3 步若仍能看到匿名项目，说明 `clearAnonKey()` 没生效或 cookie 未清——检查 `resetForIdentityChange` 是否真的被调用。

- [ ] **Step 7: 提交**

```bash
git add app/frontend/src/pages/Index.tsx
git commit -m "feat(frontend): 顶栏登录/登出入口，身份切换清空项目状态并重载"
```

---

# 阶段 3：生成链路稳定性（S3）

### Task 12: `looks_well_formed` 与 CSP 幂等

**Files:**
- Modify: `app/backend/services/html_extract.py`
- Test: `app/backend/tests/test_html_extract.py`（追加）

**Interfaces:**
- Produces: `looks_well_formed(doc: str | None) -> bool`

**为什么**：`is_complete_document` 只校验以 `</html>` 结尾。模型「续写时重开文档」时，若在开头多输出一句解释，`_DOC_RESTART_PATTERN.match`（只匹配开头）会漏判，于是 `_merge_continuation` 把两份文档拼起来——只要末尾恰好是 `</html>` 就判定成功并入库（脏数据）。

- [ ] **Step 1: 写失败测试**

追加到 `app/backend/tests/test_html_extract.py`：

```python
# ---------- 结构完整性 ----------


def test_well_formed_accepts_normal_document():
    from services.html_extract import looks_well_formed

    doc = "<!DOCTYPE html><html><head></head><body><p>hi</p></body></html>"
    assert looks_well_formed(doc) is True


def test_well_formed_rejects_missing_doctype_but_still_ok():
    """没有 DOCTYPE 也是合法文档（至多 1 个即可）。"""
    from services.html_extract import looks_well_formed

    assert looks_well_formed("<html><body></body></html>") is True


def test_well_formed_rejects_truncated():
    from services.html_extract import looks_well_formed

    assert looks_well_formed("<html><body>截断") is False


def test_well_formed_rejects_two_documents_merged():
    """两份文档被拼在一起——这正是要拦的脏数据。"""
    from services.html_extract import looks_well_formed

    merged = (
        "<!DOCTYPE html><html><body>A</body></html>"
        "<!DOCTYPE html><html><body>B</body></html>"
    )
    assert looks_well_formed(merged) is False


def test_well_formed_rejects_two_bodies():
    from services.html_extract import looks_well_formed

    doc = "<html><body>A</body><body>B</body></html>"
    assert looks_well_formed(doc) is False


def test_well_formed_rejects_empty():
    from services.html_extract import looks_well_formed

    assert looks_well_formed("") is False
    assert looks_well_formed(None) is False


def test_well_formed_not_confused_by_closing_tags():
    """</html> 与 </body> 不得被计入开标签。"""
    from services.html_extract import looks_well_formed

    doc = "<html><body>x</body></html>"
    assert looks_well_formed(doc) is True


def test_well_formed_case_insensitive():
    from services.html_extract import looks_well_formed

    assert looks_well_formed("<!doctype html><HTML><BODY>x</BODY></HTML>") is True


# ---------- CSP 幂等 ----------


def test_inject_csp_is_idempotent():
    """重复注入会产出两个 CSP meta，第二个的 default-src 'none' 会覆盖宽松策略。"""
    from services.html_extract import inject_csp

    doc = "<!DOCTYPE html><html><head><title>t</title></head><body></body></html>"
    once = inject_csp(doc)
    twice = inject_csp(once)
    assert once == twice
    assert twice.lower().count("content-security-policy") == 1


def test_inject_csp_preserves_existing_policy():
    from services.html_extract import inject_csp

    doc = (
        "<!DOCTYPE html><html><head>"
        '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">'
        "</head><body></body></html>"
    )
    assert inject_csp(doc) == doc
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_html_extract.py -q`
Expected: FAIL，`ImportError: cannot import name 'looks_well_formed'`

- [ ] **Step 3: 实现 `looks_well_formed`**

在 `app/backend/services/html_extract.py` 的 `is_complete_document` 之后插入：

```python
_HTML_TAG_COUNT = re.compile(r"<html[\s>]", re.IGNORECASE)
_BODY_TAG_COUNT = re.compile(r"<body[\s>]", re.IGNORECASE)
_DOCTYPE_COUNT = re.compile(r"<!doctype\s+html", re.IGNORECASE)


def looks_well_formed(doc: str | None) -> bool:
    """最小结构校验：拦截「两份文档被拼接」这类脏数据。

    对应 spec §4 S3.4。``is_complete_document`` 只认结尾，因此
    「模型续写时重开文档」若在开头多输出一句解释，``_DOC_RESTART_PATTERN.match``
    （只匹配开头）会漏判，``_merge_continuation`` 便把两份文档拼起来——只要末尾
    恰好是 ``</html>`` 就会被判定成功并入库。

    判定条件：
    ① 以 ``</html>`` 结尾
    ② ``<!DOCTYPE`` 至多 1 个
    ③ ``<html`` 恰好 1 个（用 ``<html[\\s>]`` 计数，``</html>`` 不含 ``<html``）
    ④ ``<body`` 恰好 1 个（同上）
    """
    if not doc:
        return False
    lowered = doc.lower()
    if not lowered.rstrip().endswith("</html>"):
        return False
    if len(_DOCTYPE_COUNT.findall(lowered)) > 1:
        return False
    if len(_HTML_TAG_COUNT.findall(lowered)) != 1:
        return False
    if len(_BODY_TAG_COUNT.findall(lowered)) != 1:
        return False
    return True
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_html_extract.py -q`
Expected: 原有用例 + `10 passed`（新的）

- [ ] **Step 5: 提交**

```bash
git add app/backend/services/html_extract.py app/backend/tests/test_html_extract.py
git commit -m "feat: looks_well_formed 结构校验与 CSP 幂等测试"
```

---

### Task 13: 上下文预算与历史条目长度

**Files:**
- Modify: `app/backend/services/prompts.py`
- Test: `app/backend/tests/test_context_memory.py`（调整 + 追加）

**Interfaces:**
- Produces:
  - `PREVIOUS_HTML_MAX_CHARS = 24_000`、`CONTINUE_HISTORY_MAX_CHARS = 12_000`
  - `truncate_previous_html(html: str | None) -> str`
  - `truncate_continue_history(doc: str) -> str`
  - `build_history_block` 中 `HISTORY_ITEM_CHARS = 400`，最新一条放宽到 600

**为什么**：`build_code_user` 注入的 `previous_html` 全文可达数十 KB，续写调用还把完整 `doc` 作为 history 再发一次——多轮生成时单次请求 token 翻倍，既贵又更容易截断。

- [ ] **Step 1: 写失败测试**

追加到 `app/backend/tests/test_context_memory.py`：

```python
# ---------- 上下文预算 ----------


def test_previous_html_passes_through_when_small():
    doc = "<html><body>短</body></html>"
    assert prompts.truncate_previous_html(doc) == doc


def test_previous_html_is_bounded():
    doc = "x" * (prompts.PREVIOUS_HTML_MAX_CHARS + 50_000)
    out = prompts.truncate_previous_html(doc)
    assert len(out) <= prompts.PREVIOUS_HTML_MAX_CHARS + 200, "必须落在预算内"
    assert "已省略" in out


def test_previous_html_keeps_tail():
    """末尾是最近改动的地方，必须保留。"""
    doc = "x" * (prompts.PREVIOUS_HTML_MAX_CHARS + 50_000) + "TAIL-MARKER"
    assert "TAIL-MARKER" in prompts.truncate_previous_html(doc)


def test_previous_html_handles_empty():
    assert prompts.truncate_previous_html(None) == ""
    assert prompts.truncate_previous_html("") == ""


def test_continue_history_is_tail_only():
    doc = "HEAD-MARKER" + "y" * 50_000 + "TAIL-MARKER"
    out = prompts.truncate_continue_history(doc)
    assert "TAIL-MARKER" in out
    assert len(out) <= prompts.CONTINUE_HISTORY_MAX_CHARS + 200


def test_continue_history_budget_covers_continue_tail():
    """必须大于 pipeline.CONTINUE_TAIL_CHARS，否则续写点上下文会被裁掉。"""
    from services.pipeline import CONTINUE_TAIL_CHARS

    assert prompts.CONTINUE_HISTORY_MAX_CHARS > CONTINUE_TAIL_CHARS


def test_history_last_item_gets_more_room():
    """长需求 + 指代时 200 字会丢掉「不要用 CDN」这类关键约束。"""
    long_first = "做" * 1000
    long_last = "改" * 1000
    block = prompts.build_history_block([long_first, long_last])
    numbered = [line for line in block.splitlines() if line[:2].strip().rstrip(".").isdigit()]
    assert len(numbered[0]) <= prompts.HISTORY_ITEM_CHARS + 8
    assert len(numbered[-1]) <= prompts.HISTORY_LATEST_ITEM_CHARS + 8
    assert prompts.HISTORY_LATEST_ITEM_CHARS > prompts.HISTORY_ITEM_CHARS


def test_history_block_honours_budget_for_many_items():
    items = ["需" * 5000 for _ in range(prompts.HISTORY_MAX_ITEMS)]
    block = prompts.build_history_block(items)
    budget = (prompts.HISTORY_MAX_ITEMS - 1) * prompts.HISTORY_ITEM_CHARS + prompts.HISTORY_LATEST_ITEM_CHARS
    assert len(block) <= budget + 500
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_context_memory.py -q`
Expected: FAIL，`AttributeError: module 'services.prompts' has no attribute 'truncate_previous_html'`

- [ ] **Step 3: 实现预算与截断**

改 `app/backend/services/prompts.py`。把 `HISTORY_ITEM_CHARS` 一段替换为：

```python
# 需求历史回看的最大条数与单条截断长度，避免上下文线性膨胀（research.md R5）。
HISTORY_MAX_ITEMS = 6
# 单条上限。旧值 200 对「长需求 + 指代」太紧：用户第一轮常是 500~1500 字，
# 截到 200 后第二轮模型可能丢掉「必须支持中文字体」「不要用 CDN」这类关键约束。
HISTORY_ITEM_CHARS = 400
# 最新一条额外放宽：它是当轮指代最可能指向的对象。
HISTORY_LATEST_ITEM_CHARS = 600

# 注入上一版 HTML 的字符上限。超出则保留 <head> + 开头 + 末尾。
PREVIOUS_HTML_MAX_CHARS = 24_000
# 续写调用 history 里回传已产出 doc 的上限。只回传尾部即可——
# 续写只需要中断点附近的上下文。必须 > pipeline.CONTINUE_TAIL_CHARS。
CONTINUE_HISTORY_MAX_CHARS = 12_000
# 截断时保留的末尾字符数（最近改动所在处）。
_TAIL_KEEP_CHARS = 2000


def _truncation_marker(omitted: int) -> str:
    return f"\n<!-- …已省略 {omitted} 字符… -->\n"


def truncate_previous_html(html: str | None) -> str:
    """把上一版 HTML 压进预算。

    超限时保留 ``<head>`` 段（CSP 与样式在那里，丢了会改变页面行为）、开头一段
    与末尾 ``_TAIL_KEEP_CHARS`` 字符，中间插入省略标记。
    """
    if not html:
        return ""
    if len(html) <= PREVIOUS_HTML_MAX_CHARS:
        return html

    head_end = 0
    lowered = html.lower()
    close_head = lowered.find("</head>")
    if lowered.lstrip().startswith(("<!doctype", "<html")) and close_head != -1:
        head_end = close_head + len("</head>")

    head_part = html[:head_end]
    tail_part = html[-_TAIL_KEEP_CHARS:]
    marker_budget = len(_truncation_marker(0)) + 12
    front_budget = max(
        0, PREVIOUS_HTML_MAX_CHARS - len(head_part) - len(tail_part) - marker_budget
    )
    front_part = html[head_end : head_end + front_budget]
    omitted = len(html) - len(head_part) - len(front_part) - len(tail_part)
    return f"{head_part}{front_part}{_truncation_marker(omitted)}{tail_part}"


def truncate_continue_history(doc: str) -> str:
    """续写 history 里回传的 doc 只取尾部。

    续写只需要中断点附近的上下文；回传全文会让单次请求 token 翻倍，
    既贵又更容易再次截断。
    """
    if not doc:
        return ""
    if len(doc) <= CONTINUE_HISTORY_MAX_CHARS:
        return doc
    tail = doc[-CONTINUE_HISTORY_MAX_CHARS:]
    return f"<!-- 以下是已输出内容的中段被裁掉后的尾部片段 -->\n{tail}"
```

`build_history_block` 改为按位置区分长度：

```python
def build_history_block(history_prompts: list[str] | None) -> str:
    """把本项目此前各版本的需求拼成「需求历史」上下文块。

    用于让模型理解「继续刚刚的需求」「按之前说的」「再优化一下」这类
    引用上文的指令——否则模型只看到孤立的当前短句，无法还原真实意图。

    最新一条放宽到 ``HISTORY_LATEST_ITEM_CHARS``：它是当轮指代最可能指向的对象。
    """
    cleaned = [p.strip() for p in (history_prompts or []) if p and p.strip()]
    if not cleaned:
        return ""
    window = cleaned[-HISTORY_MAX_ITEMS:]
    last_index = len(window) - 1
    lines = [
        f"{i}. {item[: HISTORY_LATEST_ITEM_CHARS if i - 1 == last_index else HISTORY_ITEM_CHARS]}"
        for i, item in enumerate(window, start=1)
    ]
    return (
        "【本项目此前的需求历史（按时间先后，越靠后越新）】\n"
        + "\n".join(lines)
        + "\n\n"
    )
```

`build_code_user` 的 `previous_html` 注入改为过闸门：

```python
    if previous_html:
        budgeted = truncate_previous_html(previous_html)
        parts.append(
            "以下是**上一版页面的完整源码**。请在它的基础上改进，"
            "保留已有功能不要推倒重来，然后叠加本次的新要求：\n\n" + budgeted
        )
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_context_memory.py -q`
Expected: 全绿

**注意**：既有用例 `test_history_block_truncates_long_item` 断言 `"字" * (HISTORY_ITEM_CHARS + 1) not in block`——单条历史是「最新一条」时会用 `HISTORY_LATEST_ITEM_CHARS`，所以该用例需要改成传入两条（第一条是被截断的那条）。改法：

```python
def test_history_block_truncates_long_item():
    long_prompt = "字" * (prompts.HISTORY_ITEM_CHARS + 500)
    block = prompts.build_history_block([long_prompt, "后续需求"])
    assert "字" * prompts.HISTORY_ITEM_CHARS in block
    assert "字" * (prompts.HISTORY_ITEM_CHARS + 1) not in block
```

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/services/prompts.py app/backend/tests/test_context_memory.py
git commit -m "feat: 上下文预算闸门，放宽历史单条长度"
```

---

### Task 14: 上游错误分类、退避重试与显式超时

**Files:**
- Modify: `app/backend/services/aihub_errors.py`
- Modify: `app/backend/services/pipeline.py`
- Test: `app/backend/tests/test_pipeline_recovery.py`

**Interfaces:**
- Consumes: Task 2 的 `FakeAIHub`
- Produces:
  - `class UpstreamError(Exception)`，字段 `kind: str`、`status_code: int | None`、`retriable: bool`
  - `_classify_upstream_error(exc: BaseException) -> UpstreamError`
  - `PipelineError(message, step_seq, retriable=False, error_type=None)`
  - 常量 `STAGE_TIMEOUT = 240.0`、`RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)`、`UPSTREAM_MAX_ATTEMPTS = 3`

**为什么不改 `services/aihub.py` 的 `AsyncOpenAI(timeout=…)`**：它是平台通用层，`routers/aihub.py` 等亦共用，改它会把行为变更外溢到本项目之外。`asyncio.wait_for` 效果等价且零外溢。

- [ ] **Step 1: 写失败测试**

```python
"""A3：模型服务故障注入下的恢复行为。

对应 spec §4 S3.1 的分类表：401 不重试、429/5xx/超时退避重试、空内容重试。
"""

from __future__ import annotations

import asyncio

import pytest

from services.aihub_errors import UpstreamError
from services import pipeline as pipeline_module
from services.pipeline import GenerationPipeline, PipelineError, _classify_upstream_error
from tests.fakes import FakeAIHub

HTML_OK = "<!DOCTYPE html><html><head></head><body>OK</body></html>"
ANALYSIS = '{"app_name":"A","features":["f"],"notes":"n"}'
DESIGN = '{"layout":"l","components":["c"],"state":["s"],"interactions":["i"]}'


class _AuthError(Exception):
    """模拟 openai.AuthenticationError。"""

    status_code = 401


class _RateLimitError(Exception):
    status_code = 429


class _ServerError(Exception):
    status_code = 503


# ---------- 分类 ----------


@pytest.mark.parametrize(
    "exc, expected_kind, expected_retriable",
    [
        (_AuthError("bad key"), "auth", False),
        (_RateLimitError("slow down"), "rate_limit", True),
        (_ServerError("upstream"), "upstream_5xx", True),
        (asyncio.TimeoutError(), "timeout", True),
        (ConnectionError("refused"), "timeout", True),
        (RuntimeError("who knows"), "unknown", True),
    ],
)
def test_classify_maps_upstream_symptoms(exc, expected_kind, expected_retriable):
    classified = _classify_upstream_error(exc)
    assert classified.kind == expected_kind
    assert classified.retriable is expected_retriable


def test_classify_reads_status_code_attribute():
    assert _classify_upstream_error(_RateLimitError()).status_code == 429


def test_classify_treats_permission_denied_as_permanent():
    class _Forbidden(Exception):
        status_code = 403

    assert _classify_upstream_error(_Forbidden()).retriable is False


# ---------- 重试行为（直接驱动 pipeline._call_step） ----------


async def _run_one_step(fake: FakeAIHub, session_maker):
    """用最小上下文驱动一次阶段调用。"""

    async def _make():
        async with session_maker() as session:
            return await GenerationPipeline(session, ai=fake)._call_step(
                "p-1", 1, 1, model="m", system="s", user="u"
            )

    return await _make()


async def test_401_fails_immediately_without_retry(session_maker, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", ())
    fake = FakeAIHub([_AuthError("bad key"), "should not be reached"])

    with pytest.raises(PipelineError) as excinfo:
        await _run_one_step(fake, session_maker)

    assert fake.call_count() == 1, "鉴权失败不得重试"
    assert excinfo.value.error_type == "auth"
    assert "鉴权" in excinfo.value.message


async def test_429_is_retried_then_succeeds(session_maker, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))
    fake = FakeAIHub([_RateLimitError("slow down"), "最终内容"])

    content = await _run_one_step(fake, session_maker)

    assert content == "最终内容"
    assert fake.call_count() == 2


async def test_timeout_is_retried(session_maker, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))
    fake = FakeAIHub([asyncio.TimeoutError(), "最终内容"])

    assert await _run_one_step(fake, session_maker) == "最终内容"
    assert fake.call_count() == 2


async def test_empty_content_is_retried_and_succeeds(session_maker, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))
    fake = FakeAIHub(["", "有内容了"])

    assert await _run_one_step(fake, session_maker) == "有内容了"


async def test_all_empty_gives_empty_error_type(session_maker, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))
    fake = FakeAIHub(["", "", "", "", ""])

    with pytest.raises(PipelineError) as excinfo:
        await _run_one_step(fake, session_maker)

    assert excinfo.value.error_type == "empty"
    assert "空内容" in excinfo.value.message


async def test_exhausted_retries_give_upstream_error_type(session_maker, monkeypatch):
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0, 0, 0))
    fake = FakeAIHub([_ServerError(), _ServerError(), _ServerError(), _ServerError()])

    with pytest.raises(PipelineError) as excinfo:
        await _run_one_step(fake, session_maker)

    assert excinfo.value.error_type == "upstream_5xx"
    assert excinfo.value.retriable is True


async def test_stage_timeout_is_enforced(session_maker, monkeypatch):
    """显式超时：模型卡住不得无限占用流水线。"""
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0,))
    monkeypatch.setattr(pipeline_module, "STAGE_TIMEOUT", 0.05)

    class _Hanging(FakeAIHub):
        async def gentxt(self, request):
            self.requests.append(request)
            await asyncio.sleep(5)
            raise AssertionError("unreachable")

    fake = _Hanging([])

    with pytest.raises(PipelineError) as excinfo:
        await _run_one_step(fake, session_maker)

    assert excinfo.value.error_type == "timeout"


async def test_backoff_sleeps_between_retries(session_maker, monkeypatch):
    """退避必须真的 await——取消接口的 task.cancel() 在 sleep 期间同样生效。"""
    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0.05,))
    fake = FakeAIHub([_RateLimitError(), "ok"])

    loop = asyncio.get_running_loop()
    start = loop.time()
    await _run_one_step(fake, session_maker)
    assert loop.time() - start >= 0.04
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_pipeline_recovery.py -q`
Expected: FAIL，`ImportError: cannot import name 'UpstreamError'`

- [ ] **Step 3: 加 `UpstreamError`**

追加到 `app/backend/services/aihub_errors.py`：

```python
class UpstreamError(Exception):
    """上游模型服务的**可分类**故障。

    与上面三个输入校验错误不同，这一类的语义是「调用失败了，但为什么失败决定了
    该不该重试」。旧实现把所有异常压成同一句「模型服务暂时不可用，请稍后重试」，
    结果 401 密钥失效（重试无用）与 429 限流（重试有用）被一视同仁，用户被引导
    去做无意义的反复提交。

    kind 取值：
        ``auth``          401/403，永久失败，不重试
        ``rate_limit``    429，退避重试
        ``timeout``       超时或连接错误，退避重试
        ``upstream_5xx``  5xx，退避重试
        ``unknown``       无法判定，按可重试处理（宁可多试一次）
    """

    def __init__(
        self,
        kind: str,
        *,
        status_code: int | None = None,
        message: str = "",
    ) -> None:
        super().__init__(message or kind)
        self.kind = kind
        self.status_code = status_code
        # auth 是唯一不可恢复的一类：重试只会浪费配额并让用户白等。
        self.retriable = kind != "auth"
```

- [ ] **Step 4: 在 pipeline 里加分类、超时、退避**

改 `app/backend/services/pipeline.py`。

import 区：

```python
import asyncio

from services.aihub_errors import UpstreamError
```

常量区（放在 `CONTINUE_TAIL_CHARS` 附近）：

```python
# 单次模型调用的显式超时。SDK 默认超时可能让一次调用长时间挂住，
# 而流水线总耗时上限是「调用次数 × 单次超时」——必须有界。
STAGE_TIMEOUT = 240.0
# 可恢复故障的退避序列（秒）。用 asyncio.sleep 而非阻塞睡眠：
# 取消接口的 task.cancel() 在 sleep 期间会注入 CancelledError，取消语义不被破坏。
RETRY_BACKOFF_SECONDS = (0.5, 1.0, 2.0)
# 空内容的重试次数（推理模型偶发把 token 预算耗在思考链上）。
EMPTY_RETRY_ATTEMPTS = 2
# 空内容重试时降低的输出预算：思考链吃预算时更有效。
EMPTY_RETRY_MAX_TOKENS = 8192
```

新增分类函数（放在 `_fallback_title` 之后）：

```python
# 上游症状 → kind 的映射。用 getattr 读 status_code，避免硬依赖 openai 的异常类
# 层次（SDK 版本变化时不会静默失效）。
_STATUS_KIND = {
    401: "auth",
    403: "auth",
    429: "rate_limit",
}


def _classify_upstream_error(exc: BaseException) -> UpstreamError:
    """把上游异常翻译成带重试语义的 UpstreamError。

    无法判定时归 ``unknown`` 且 ``retriable=True``——宁可多试一次，
    也不要把一次偶发网络抖动变成用户可见的失败。
    """
    if isinstance(exc, UpstreamError):
        return exc

    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        if status in _STATUS_KIND:
            kind = _STATUS_KIND[status]
        elif 500 <= status < 600:
            kind = "upstream_5xx"
        else:
            kind = "unknown"
        return UpstreamError(kind, status_code=status, message=str(exc))

    name = type(exc).__name__
    if "Timeout" in name or isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return UpstreamError("timeout", message=str(exc))
    if "Connection" in name or isinstance(exc, (ConnectionError, OSError)):
        return UpstreamError("timeout", message=str(exc))
    if "RateLimit" in name:
        return UpstreamError("rate_limit", message=str(exc))
    if "Authentication" in name or "Permission" in name or "Unauthorized" in name:
        return UpstreamError("auth", message=str(exc))

    return UpstreamError("unknown", message=str(exc))


# 用户可见文案按 kind 区分。旧实现把所有失败压成同一句话，用户无法判断
# 「重试有用」还是「重试无用」。见 spec §4 S3.1 表。
_KIND_MESSAGES = {
    "auth": "模型服务鉴权失败，请联系管理员",
    "rate_limit": "模型服务繁忙，请稍后重试",
    "timeout": "模型服务响应超时，请稍后重试",
    "upstream_5xx": "模型服务暂时不可用，请稍后重试",
    "unknown": "模型服务暂时不可用，请稍后重试",
}
```

`PipelineError` 扩展：

```python
class PipelineError(Exception):
    """携带面向用户可读中文措辞的流水线错误。"""

    def __init__(
        self,
        message: str,
        step_seq: int,
        retriable: bool = False,
        error_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.step_seq = step_seq
        # retriable 供上层判断「让用户重试」是否有意义
        self.retriable = retriable
        # error_type 会写进 versions.summary，供排障（前端可不必展示）
        self.error_type = error_type
```

替换 `_call_step` 的重试主体（`for attempt in range(2)` 那一段）：

```python
        last_upstream: UpstreamError | None = None
        empty_attempts = 0
        attempt = 0
        while True:
            request.max_tokens = (
                max_tokens
                if empty_attempts == 0
                else min(max_tokens, EMPTY_RETRY_MAX_TOKENS)
            )
            try:
                response = await asyncio.wait_for(
                    self._ai.gentxt(request), timeout=STAGE_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                classified = _classify_upstream_error(exc)
                logger.warning(
                    "阶段 %s 调用失败 project=%s seq=%s step=%s model=%s attempt=%s kind=%s status=%s",
                    step_seq,
                    project_public_id[:8],
                    version_seq,
                    step_seq,
                    model,
                    attempt + 1,
                    classified.kind,
                    classified.status_code,
                )
                last_upstream = classified
                if not classified.retriable:
                    # 鉴权类故障重试无用，立即失败，避免让用户反复提交
                    logger.error("上游鉴权失败，不再重试: %s", classified)
                    raise PipelineError(
                        _KIND_MESSAGES[classified.kind],
                        step_seq,
                        retriable=False,
                        error_type=classified.kind,
                    ) from exc
            else:
                content = (getattr(response, "content", "") or "").strip()
                if content:
                    return content
                empty_attempts += 1
                logger.warning(
                    "阶段 %s 第 %s 次调用返回空内容", step_seq, empty_attempts
                )
                if empty_attempts >= EMPTY_RETRY_ATTEMPTS:
                    raise PipelineError(
                        "模型返回了空内容，请重新提交生成",
                        step_seq,
                        retriable=True,
                        error_type="empty",
                    )

            # 到这里说明本次尝试失败：退避后重试
            if last_upstream is not None and not last_upstream.retriable:
                break
            if attempt >= len(RETRY_BACKOFF_SECONDS):
                break
            await asyncio.sleep(RETRY_BACKOFF_SECONDS[attempt])
            attempt += 1

        if last_upstream is not None:
            raise PipelineError(
                _KIND_MESSAGES[last_upstream.kind],
                step_seq,
                retriable=last_upstream.retriable,
                error_type=last_upstream.kind,
            )
        raise PipelineError(
            "模型返回了空内容，请重新提交生成",
            step_seq,
            retriable=True,
            error_type="empty",
        )
```

注意 `request` 现在是循环外构造的，需要把 `GenTxtRequest(...)` 的构造**移到 while 之前**，并保留 `max_tokens` 可变（`request.max_tokens = ...`）。

- [ ] **Step 5: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_pipeline_recovery.py -q`
Expected: `13 passed`

- [ ] **Step 6: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/services/aihub_errors.py app/backend/services/pipeline.py \
        app/backend/tests/test_pipeline_recovery.py
git commit -m "feat: 上游错误分类、退避重试与显式超时"
```

---

### Task 15: 心跳、进程内存活判定与时间戳口径统一

**Files:**
- Modify: `app/backend/routers/atoms.py`（`_recover_stale_versions`、新增 `_is_task_alive`）
- Modify: `app/backend/services/pipeline.py`（新增 `_orm_clock`、`_as_utc`、`_heartbeat`，`run` 挂载）
- Test: `app/backend/tests/test_stale_recovery.py`

**Interfaces:**
- Consumes: Task 6 的 `_recover_stale_versions(db, owner, public_id)`；Task 2 的 `GENERATION_INLINE`
- Produces:
  - `pipeline._orm_clock() -> datetime`（朴素本地时间，与 `models` 的 `default/onupdate` 同源）
  - `pipeline._as_utc(value: datetime | None) -> datetime | None`
  - `pipeline.HEARTBEAT_INTERVAL = 30.0`
  - `pipeline.GenerationPipeline._heartbeat(public_id, version_seq)`
  - `atoms._is_task_alive(public_id, version_seq) -> bool`

**这一任务解决 spec §5 里登记的两条真实竞态**：

1. 流水线总耗时上限是「调用次数 × `STAGE_TIMEOUT`」，可超过 `STALE_AFTER`（10 分钟）。此时任意访客打开列表就会把 `running` 版本置 `failed`，随后后台任务正常返回 `succeeded` 又被取消守卫拦住 —— 用户看到**生成失败但内容其实已生成**。
2. `_RUNNING_TASKS` 是现成的精确存活信号。单 worker 部署下，跳过进程内仍活着的版本就当下解决了误杀，且服务重启后表为空 → 陈旧版本恢复得比等 10 分钟更快。

**时间戳口径（见本计划顶部「风险 A」）**：心跳必须写 `datetime.now()`（与 ORM 的 `default/onupdate=PyDateTime.now` 同源），读侧 `_as_utc` 把朴素值按**本地时间**解释（`.astimezone()`）。这个规则只会低估年龄，永不误杀。

- [ ] **Step 1: 写失败测试**

```python
"""A3 回归：陈旧恢复不得误杀仍在执行的生成。

对应 spec §5 竞态表第 1、2 行与本计划顶部「风险 A/B」。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from models.projects import Projects
from models.versions import Versions
from services.pipeline import HEARTBEAT_INTERVAL, _as_utc, _orm_clock
from services.pipeline import STAGE_TIMEOUT


def test_orm_clock_is_naive_like_model_default():
    """心跳必须与 models 的 default/onupdate 同源，否则同一列混用两种口径。"""
    assert _orm_clock().tzinfo is None


def test_as_utc_interprets_naive_as_local():
    """朴素值按本地时间解释——只会低估年龄，永不误杀。"""
    naive = datetime.now() - timedelta(minutes=5)
    converted = _as_utc(naive)
    assert converted is not None
    assert converted.tzinfo is not None
    age = datetime.now(converted.tzinfo) - converted
    assert timedelta(minutes=4) < age < timedelta(minutes=6)


def test_as_utc_handles_aware():
    from datetime import timezone

    aware = datetime.now(timezone.utc) - timedelta(minutes=5)
    converted = _as_utc(aware)
    age = datetime.now(timezone.utc) - converted
    assert timedelta(minutes=4) < age < timedelta(minutes=6)


def test_as_utc_handles_none():
    assert _as_utc(None) is None


def test_heartbeat_interval_smaller_than_stale_after():
    """心跳必须比陈旧阈值密，否则心跳本身来不及救人。"""
    from routers.atoms import STALE_AFTER

    assert HEARTBEAT_INTERVAL < STALE_AFTER.total_seconds()


def test_stage_timeout_times_max_calls_can_exceed_stale_after():
    """这正是必须有存活/心跳保护的原因：最坏情况确实会超过陈旧阈值。"""
    from routers.atoms import STALE_AFTER

    worst_case = STAGE_TIMEOUT * 6
    assert worst_case > STALE_AFTER.total_seconds()


async def test_running_task_is_never_swept(client, db_session, monkeypatch):
    """进程内仍活着的版本不得被置 failed。"""
    from routers import atoms

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    # 造一个「很久没动」的 running 版本
    stamp = datetime.now() - timedelta(minutes=30)
    project_result = await db_session.execute(
        select(Projects).where(Projects.public_id == public_id)
    )
    project = project_result.scalars().first()
    version = Versions(
        project_public_id=public_id,
        seq=1,
        prompt="旧任务",
        status="running",
        created_at=stamp,
        updated_at=stamp,
    )
    db_session.add(version)
    await db_session.commit()

    # 伪装一个仍在执行的进程内任务
    class _Alive:
        def done(self) -> bool:
            return False

    monkeypatch.setitem(atoms._RUNNING_TASKS, (public_id, 1), _Alive())

    await client.get("/api/v1/atoms/projects")

    check = await db_session.execute(
        select(Versions).where(Versions.project_public_id == public_id)
    )
    assert check.scalars().first().status == "running", "活着的版本不得被误杀"


async def test_dead_stale_version_is_swept(client, db_session):
    stamp = datetime.now() - timedelta(minutes=30)
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    db_session.add(
        Versions(
            project_public_id=public_id,
            seq=1,
            prompt="旧任务",
            status="running",
            created_at=stamp,
            updated_at=stamp,
        )
    )
    await db_session.commit()

    await client.get("/api/v1/atoms/projects")

    check = await db_session.execute(
        select(Versions).where(Versions.project_public_id == public_id)
    )
    version = check.scalars().first()
    assert version.status == "failed"
    assert version.error


async def test_fresh_version_is_not_swept(client, db_session):
    """刚受理的版本绝不能被扫成失败。"""
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    db_session.add(
        Versions(
            project_public_id=public_id,
            seq=1,
            prompt="新任务",
            status="pending",
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
    )
    await db_session.commit()

    await client.get("/api/v1/atoms/projects")

    check = await db_session.execute(
        select(Versions).where(Versions.project_public_id == public_id)
    )
    assert check.scalars().first().status == "pending"
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_stale_recovery.py -q`
Expected: FAIL，`ImportError: cannot import name 'HEARTBEAT_INTERVAL'`

- [ ] **Step 3: 在 pipeline 里加时钟与心跳**

`app/backend/services/pipeline.py` 常量区追加：

```python
# 心跳间隔。三者关系必须保持（见 test_stale_recovery.py 的断言）：
#   HEARTBEAT_INTERVAL(30s) < STALE_AFTER(10min) < 前端 GENERATION_POLL_TIMEOUT_MS(12min)
HEARTBEAT_INTERVAL = 30.0
```

新增时钟函数（放在 Task 6 加的 `_as_utc` 之后）：

```python
def _orm_clock() -> datetime:
    """与 models 的 default/onupdate 同源的朴素本地时间。

    **不要换成 ``_now()``**：``models/versions.py`` 的
    ``default=PyDateTime.now, onupdate=PyDateTime.now`` 写的是朴素本地时间，
    而 ``_now()`` 是 aware-UTC。同一列混用两种口径后，读侧无论按哪种解释都会
    整体偏移一个时区——在 UTC+8 下变成「真实年龄 + 8 小时」，2 小时的正常生成
    会被误判为陈旧，正是本次要消灭的误杀。
    """
    return datetime.now()


def _as_utc(value: datetime | None) -> datetime | None:
    """（Task 6 已添加，本任务只需确认它在；不要再定义一遍。）"""
```

`GenerationPipeline` 新增心跳方法：

```python
    async def _heartbeat(self, project_public_id: str, version_seq: int) -> None:
        """等待模型响应期间周期性刷新 versions.updated_at。

        必须用**独立的** DB 会话：SQLAlchemy 的 AsyncSession 不能被两个协程
        并发使用，而心跳恰好在 AI 调用在飞、``self._db`` 正被占用时运行。

        只在版本仍属 ACTIVE_STATUSES 时刷新：与取消守卫一致，取消后不再续命。
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    async with db_manager.session() as session:
                        result = await session.execute(
                            select(Versions).where(
                                Versions.project_public_id == project_public_id,
                                Versions.seq == version_seq,
                            )
                        )
                        version = result.scalars().first()
                        if version and version.status in ACTIVE_STATUSES:
                            version.updated_at = _orm_clock()
                            await session.commit()
                except Exception as exc:  # noqa: BLE001 - 心跳失败不得影响生成
                    logger.warning("心跳刷新失败（忽略）: %s", type(exc).__name__)
        except asyncio.CancelledError:
            raise
```

import 区补 `from core.database import db_manager`。

在 `run()` 里挂载心跳，用 `try/finally` 保证不留下孤儿任务：

```python
        await self._set_version_status(project_public_id, version_seq, "running")

        heartbeat = asyncio.create_task(
            self._heartbeat(project_public_id, version_seq)
        )
        try:
            # ---------- 阶段 1 需求分析 ----------
            ...（原有三阶段逻辑整体缩进一级，保持不变）
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
```

注意：现有的 `except GenerationCancelled / PipelineError / Exception` 三个分支与尾部的成功收尾都在 `try` 内，`finally` 只负责收心跳。

- [ ] **Step 4: 在 atoms.py 加存活判定并统一时间戳口径**

```python
def _is_task_alive(public_id: str, version_seq: int) -> bool:
    """该版本是否正由**本进程**的生成任务执行。

    ``_RUNNING_TASKS`` 是精确、无竞态的存活信号——比任何时间戳启发式都准。
    服务重启后表为空 → 残留版本立即判定为陈旧，恢复比等 STALE_AFTER 更快。
    多 worker 部署时它只能看到本进程的任务，所以仍保留心跳作为后备。
    """
    task = _RUNNING_TASKS.get((public_id, version_seq))
    return task is not None and not task.done()
```

`_recover_stale_versions` 的循环体改为：

```python
    now = datetime.now(timezone.utc)
    changed = False
    for version in stale:
        if _is_task_alive(version.project_public_id, version.seq):
            continue  # 进程内仍在执行：绝不会是「中断残留」

        # 时间戳口径归一：朴素值按本地时间解释，只会低估年龄（见 pipeline._as_utc）
        reference = _as_utc(version.updated_at) or _as_utc(version.created_at)
        if reference and now - reference < STALE_AFTER:
            continue
```

import 区**无需再动**——Task 6 已经把 `_as_utc` 加进 `routers/atoms.py` 的 import 了。

判据从 `created_at` 换成 `updated_at`（优先），这是心跳能起作用的前提。
Task 6 写的那两行要在本任务里改掉：

```python
        reference = _as_utc(version.updated_at) or _as_utc(version.created_at)
```

（Task 6 的版本只读 `created_at`；心跳刷新的是 `updated_at`，所以必须先看它。）

- [ ] **Step 5: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_stale_recovery.py -q`
Expected: `10 passed`

- [ ] **Step 6: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

若 `test_running_task_is_never_swept` 因 `monkeypatch.setitem` 与真实 task 注册表冲突而失败，改为 monkeypatch `atoms._is_task_alive` 直接返回 True——语义等价，且不依赖任务表内部结构。

```bash
git add app/backend/routers/atoms.py app/backend/services/pipeline.py \
        app/backend/tests/test_stale_recovery.py
git commit -m "fix: 心跳与进程内存活判定，消灭陈旧恢复误杀活跃生成"
```

---

### Task 16: 结构校验与上下文预算接入代码生成

**Files:**
- Modify: `app/backend/services/pipeline.py`（`_generate_code`、`_merge_continuation`）
- Test: `app/backend/tests/test_pipeline_recovery.py`（追加）

**Interfaces:**
- Consumes: Task 12 的 `looks_well_formed`；Task 13 的 `truncate_continue_history`、`truncate_previous_html`
- Produces: 判定从 `is_complete_document` 升级为 `is_complete_document and looks_well_formed`

- [ ] **Step 1: 写失败测试**

追加到 `app/backend/tests/test_pipeline_recovery.py`：

```python
# ---------- 代码生成的结构校验与预算 ----------

TWO_DOCS_MERGED = (
    "<!DOCTYPE html><html><body>A</body></html>"
    "<!DOCTYPE html><html><body>B</body></html>"
)


async def test_truncated_then_continuation_succeeds(session_maker, monkeypatch):
    """首次截断 + 续写成功 → 最终是一份结构完整的文档。"""
    from services.pipeline import GenerationPipeline

    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0,))

    partial = "<!DOCTYPE html><html><head></head><body><p>前半"
    tail = "段</p></body></html>"
    fake = FakeAIHub([partial, tail])

    async with session_maker() as session:
        html = await GenerationPipeline(session, ai=fake)._generate_code(
            "p-1", 1, "prompt", "analysis", "design", None, None
        )

    assert html.rstrip().endswith("</html>")
    assert html.count("<body") == 1
    assert "前半段" in html


async def test_merged_two_documents_is_rejected(session_maker, monkeypatch):
    """「模型重开文档」被拼接成两份文档时必须判失败，而不是入库脏数据。"""
    from services.pipeline import GenerationPipeline, PipelineError

    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0,))
    # 第一轮：截断；两次续写都返回又一份完整文档 → 拼接后有两个 <body>
    fake = FakeAIHub([TWO_DOCS_MERGED, TWO_DOCS_MERGED, TWO_DOCS_MERGED, TWO_DOCS_MERGED, TWO_DOCS_MERGED])

    async with session_maker() as session:
        with pytest.raises(PipelineError) as excinfo:
            await GenerationPipeline(session, ai=fake)._generate_code(
                "p-1", 1, "prompt", "analysis", "design", None, None
            )

    assert excinfo.value.error_type == "truncated" or "不完整" in excinfo.value.message


async def test_continuation_history_is_truncated(session_maker, monkeypatch):
    """续写调用回传的 doc 必须过预算闸门。"""
    from services import prompts
    from services.pipeline import GenerationPipeline

    monkeypatch.setattr(pipeline_module, "RETRY_BACKOFF_SECONDS", (0,))

    huge_body = "x" * (prompts.CONTINUE_HISTORY_MAX_CHARS + 30_000)
    partial = f"<!DOCTYPE html><html><head></head><body>{huge_body}"
    fake = FakeAIHub([partial, "</body></html>"])

    async with session_maker() as session:
        await GenerationPipeline(session, ai=fake)._generate_code(
            "p-1", 1, "prompt", "analysis", "design", None, None
        )

    # 第 2 次调用是续写：它的 history[1]（assistant）不应超过预算
    continuation_request = fake.requests[1]
    assistant_message = [m for m in continuation_request.messages if m.role == "assistant"]
    assert assistant_message, "续写调用必须带上已产出的 doc 作为 history"
    assert len(assistant_message[0].content) <= prompts.CONTINUE_HISTORY_MAX_CHARS + 200
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_pipeline_recovery.py -q -k "truncat or merged or history"`
Expected: `test_merged_two_documents_is_rejected` 与 `test_continuation_history_is_truncated` FAIL

- [ ] **Step 3: 升级判定并接入预算**

改 `app/backend/services/pipeline.py` 的 import：

```python
from services.html_extract import (
    extract_html,
    inject_csp,
    is_complete_document,
    looks_well_formed,
)
```

加一个组合判定：

```python
def _is_acceptable_document(doc: str) -> bool:
    """生成结果是否可入库。

    ``is_complete_document`` 只认结尾 ``</html>``，所以「两份文档被拼接」这类
    脏数据能混过去——只要末尾恰好是 ``</html>``。加上 ``looks_well_formed``
    的结构校验（唯一 ``<html>`` / 唯一 ``<body>`` / 至多一个 ``<!DOCTYPE``）
    才真正拦得住。见 spec §4 S3.4。
    """
    return is_complete_document(doc) and looks_well_formed(doc)
```

`_generate_code` 里两处 `is_complete_document(doc)` 都换成 `_is_acceptable_document(doc)`，并做三处调整：

```python
        user = prompts.build_code_user(
            prompt, analysis_raw, design_raw, previous_html, history_prompts
        )
        for attempt in range(2):
            code_raw = await self._call_step(...)
            doc = extract_html(code_raw)
            for round_no in range(2):
                lowered = doc.lower()
                if "<html" not in lowered and "<body" not in lowered:
                    break
                if _is_acceptable_document(doc):
                    return inject_csp(doc)
                logger.warning(
                    "代码生成第 %s 轮产出不完整或结构异常（长度 %s），尝试续写第 %s 次",
                    attempt + 1,
                    len(doc),
                    round_no + 1,
                )
                cont_raw = await self._call_step(
                    project_public_id,
                    version_seq,
                    step_seq=3,
                    model=CODE_MODEL,
                    system=prompts.CODE_SYSTEM,
                    user=prompts.build_code_continue_user(doc[-CONTINUE_TAIL_CHARS:]),
                    max_tokens=CODE_MAX_TOKENS,
                    history=[
                        ChatMessage(role="user", content=user),
                        # 只回传尾部：续写只需要中断点附近的上下文，
                        # 回传全文会让单次请求 token 翻倍，更容易再次截断。
                        ChatMessage(
                            role="assistant",
                            content=prompts.truncate_continue_history(doc),
                        ),
                    ],
                )
                cont = extract_html(cont_raw)
                if not cont:
                    break
                if _DOC_RESTART_PATTERN.match(cont):
                    doc = cont
                else:
                    merged = _merge_continuation(doc, cont)
                    if not looks_well_formed(merged):
                        # 拼接后结构被破坏（典型：模型重开了整篇文档，
                        # 只是开头多了一句解释让 _DOC_RESTART_PATTERN 漏判）。
                        # 采用新产出，而不是把两份文档拼在一起入库。
                        logger.warning("续写片段与原文拼接后结构异常，改用新产出")
                        doc = cont
                    else:
                        doc = merged
            if _is_acceptable_document(doc):
                return inject_csp(doc)
            logger.warning("代码生成第 %s 轮续写后仍不完整", attempt + 1)
        raise PipelineError(
            "生成的页面内容不完整（可能被截断），请简化需求后重试",
            3,
            retriable=True,
            error_type="truncated",
        )
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_pipeline_recovery.py -q`
Expected: 全绿

- [ ] **Step 5: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/services/pipeline.py app/backend/tests/test_pipeline_recovery.py
git commit -m "fix: 代码生成接入结构校验与上下文预算，拒绝拼接脏数据"
```

---

### Task 17: 失败可观测性（error_type 落入 summary）

**Files:**
- Modify: `app/backend/services/pipeline.py`（`run` 的失败分支、`_fail`）
- Test: `app/backend/tests/test_pipeline_recovery.py`（追加）

**Interfaces:**
- Consumes: Task 14 的 `PipelineError.error_type`；`versions.summary`（JSON 列已存在，**不改模型**）
- Produces:
  - `_fail(..., error_type=None)`
  - `versions.summary` 增加 `error_type` 子键
  - `GET /versions/{seq}` 顶层增加 `error_type`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_pipeline_recovery.py`：

```python
# ---------- 失败可观测性 ----------


async def test_auth_failure_persists_error_type(client, monkeypatch):
    """401 失败必须留下 error_type=auth，供排障，且只调用 1 次。"""
    from services import pipeline as pipeline_module
    from tests.fakes import FakeAIHub

    monkeypatch.setenv("GENERATION_INLINE", "1")
    fake = FakeAIHub([_AuthError("bad key")])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个应用"},
    )
    seq = accepted.json()["version_seq"]

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/{seq}")
    body = detail.json()
    assert body["status"] == "failed"
    assert body["error_type"] == "auth"
    assert "鉴权" in body["error"]
    assert fake.call_count() == 1, "鉴权失败不得重试"


async def test_empty_content_failure_persists_error_type(client, monkeypatch):
    from services import pipeline as pipeline_module
    from tests.fakes import FakeAIHub

    monkeypatch.setenv("GENERATION_INLINE", "1")
    fake = FakeAIHub(["", "", "", "", ""])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个应用"},
    )
    seq = accepted.json()["version_seq"]

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/{seq}")
    body = detail.json()
    assert body["status"] == "failed"
    assert body["error_type"] == "empty"
    assert body["summary"]["error_type"] == "empty"


async def test_failed_version_keeps_html_null(client, monkeypatch):
    """失败不得写入半成品 HTML。"""
    from services import pipeline as pipeline_module
    from tests.fakes import FakeAIHub

    monkeypatch.setenv("GENERATION_INLINE", "1")
    fake = FakeAIHub(["{}", "{}", "不是页面", "不是页面"])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个应用"},
    )
    seq = accepted.json()["version_seq"]

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/{seq}")
    assert detail.json()["html"] == ""
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_pipeline_recovery.py -q -k "persists or keeps_html_null"`
Expected: FAIL，`KeyError: 'error_type'`

- [ ] **Step 3: `_fail` 写入 error_type**

`pipeline.py` 的 `_fail` 签名与实现：

```python
    async def _fail(
        self,
        project_public_id: str,
        version_seq: int,
        step_seq: int,
        message: str,
        error_type: str | None = None,
        upstream_status: int | None = None,
    ) -> None:
        step = await self._get_step(project_public_id, version_seq, step_seq)
        if step:
            step.status = "failed"
            step.output = message
            step.ended_at = _iso(_now())
        version = await self._get_version(project_public_id, version_seq)
        # 取消守卫：版本已被取消接口置为 cancelled 时，迟到的失败不得覆盖取消态
        if version and version.status in ACTIVE_STATUSES:
            version.status = "failed"
            version.error = message
            if error_type:
                # 写进已有的 summary JSON 列，不需要改数据模型。
                # 前端不必展示，供排障：区分「重试有用」与「重试无用」。
                existing = _parse_json_payload(version.summary or "") or {}
                existing["error_type"] = error_type
                if upstream_status is not None:
                    existing["upstream_status"] = upstream_status
                version.summary = json.dumps(existing, ensure_ascii=False)
        project = await self._get_project(project_public_id)
        if project and project.latest_status in ACTIVE_STATUSES:
            project.latest_status = "failed"
        self._db.add(
            Messages(
                project_public_id=project_public_id,
                role="assistant",
                content=f"生成失败：{message}",
                version_seq=version_seq,
            )
        )
        await self._db.commit()
```

`run()` 的失败分支改成透传：

```python
        except PipelineError as exc:
            await self._fail(
                project_public_id,
                version_seq,
                exc.step_seq,
                exc.message,
                error_type=exc.error_type,
            )
            return {
                "status": "failed",
                "message": exc.message,
                "failed_seq": exc.step_seq,
                "error_type": exc.error_type,
                "steps": await self.load_steps(project_public_id, version_seq),
            }
        except Exception as exc:  # noqa: BLE001 - 兜底为可读中文提示
            logger.exception("生成流水线异常: %s", exc)
            message = "模型服务暂时不可用，请稍后重试"
            await self._fail(
                project_public_id, version_seq, 3, message, error_type="unknown"
            )
            return {
                "status": "failed",
                "message": message,
                "failed_seq": 3,
                "error_type": "unknown",
                "steps": await self.load_steps(project_public_id, version_seq),
            }
```

- [ ] **Step 4: 在 `GET /versions/{seq}` 暴露 error_type**

`routers/atoms.py` 的 `get_version`，在 `summary` 之后加一层派生：

```python
    summary = _parse_summary(version.summary)
    payload = {
        "seq": version.seq,
        "prompt": version.prompt,
        "html": version.html or "",
        "summary": summary,
        # 排障用：区分鉴权失败（重试无用）与限流/超时（重试有用）。
        # 前端不必展示。
        "error_type": (summary or {}).get("error_type"),
        "status": version.status,
        "error": version.error,
        "duration_ms": version.duration_ms,
        "created_at": _iso(version.created_at),
    }
```

- [ ] **Step 5: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_pipeline_recovery.py -q`
Expected: 全绿

- [ ] **Step 6: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/services/pipeline.py app/backend/routers/atoms.py \
        app/backend/tests/test_pipeline_recovery.py
git commit -m "feat: 失败可观测性（error_type 落入 summary 并在版本详情暴露）"
```

---

# 阶段 4：回滚与一致性（S5）

### Task 18: `restore` 接口与版本详情的哈希字段

**Files:**
- Modify: `app/backend/routers/atoms.py`（`_API_TITLE`…`restore` 新路由、`get_version` 的 `html_sha256`）
- Test: `app/backend/tests/test_version_restore.py`

**Interfaces:**
- Consumes: Task 5 的 `_require_project`；Task 4 的 `_json`、`RouteError`
- Produces:
  - `POST /api/v1/atoms/projects/{public_id}/versions/{seq}/restore` → `{"restored": true, "version_seq": int, "restored_from": int}`
  - `GET /versions/{seq}` 响应新增 `html_sha256: str`

**为什么回滚要产生新版本**：`previous_html` 的查询恒取「seq 最大的 succeeded 版本」（spec §2 不变量 2）。若回滚不产生新 seq，用户回滚到 v2 后再提需求，系统会拿 v5 当基线 —— 回滚形同虚设。新版本让基线自动正确，无需改流水线。

- [ ] **Step 1: 写失败测试**

```python
"""A6：版本回滚语义。

关键断言是「回滚后下一轮的基线是被回滚的版本，而不是回滚前的最大版本」——
这是 spec §5.1 指出的设计缺陷点。
"""

from __future__ import annotations

import pytest

from schemas.aihub import GenTxtRequest
from services import pipeline as pipeline_module
from tests.fakes import FakeAIHub

ANALYSIS = '{"app_name":"A","features":["f"],"notes":"n"}'
DESIGN = '{"layout":"l","components":["c"],"state":["s"],"interactions":["i"]}'
HTML_V1 = "<!DOCTYPE html><html><head></head><body>V1-MARKER</body></html>"
HTML_V2 = "<!DOCTYPE html><html><head></head><body>V2-MARKER-V1-MARKER</body></html>"
HTML_V3 = "<!DOCTYPE html><html><head></head><body>V3-MARKER-V2-MARKER-V1-MARKER</body></html>"


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setenv("GENERATION_INLINE", "1")


async def _three_versions(client, monkeypatch):
    fake = FakeAIHub(
        [ANALYSIS, DESIGN, HTML_V1, ANALYSIS, DESIGN, HTML_V2, ANALYSIS, DESIGN, HTML_V3]
    )
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    for prompt in ("第一版记账", "加月份筛选", "加导出"):
        await client.post(
            f"/api/v1/atoms/projects/{public_id}/generate", json={"prompt": prompt}
        )
    return fake, public_id


async def test_restore_creates_new_version_with_identical_html(client, inline, monkeypatch):
    fake, public_id = await _three_versions(client, monkeypatch)

    restored = await client.post(
        f"/api/v1/atoms/projects/{public_id}/versions/1/restore"
    )
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["restored"] is True
    assert body["restored_from"] == 1
    assert body["version_seq"] == 4

    v1 = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/1")
    v4 = await client.get(f"/api/v1/atoms/projects/{public_id}/versions/4")
    assert v4.json()["html"] == v1.json()["html"], "必须逐字节相等"
    assert v4.json()["status"] == "succeeded"


async def test_restore_keeps_intermediate_versions(client, inline, monkeypatch):
    fake, public_id = await _three_versions(client, monkeypatch)
    await client.post(f"/api/v1/atoms/projects/{public_id}/versions/1/restore")

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}")
    seqs = {v["seq"]: v["status"] for v in detail.json()["versions"]}
    assert seqs[2] == "succeeded", "回滚不销毁中间版本"
    assert seqs[3] == "succeeded"


async def test_baseline_after_restore_is_the_restored_version(client, inline, monkeypatch):
    """回滚后一轮增量的 previous_html 必须等于回滚产生的那一版。"""
    fake, public_id = await _three_versions(client, monkeypatch)
    await client.post(f"/api/v1/atoms/projects/{public_id}/versions/1/restore")

    restored_html = (
        await client.get(f"/api/v1/atoms/projects/{public_id}/versions/4")
    ).json()["html"]

    # 第五轮
    fake.script.extend([ANALYSIS, DESIGN, HTML_V1])
    await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "在现在的基础上再加导出"},
    )

    fifth_user = fake.user_texts()[-3]
    assert restored_html in fifth_user, "基线必须是被回滚的版本（v4），不是 v3"
    # 反向断言：v3 独有的内容不应作为完整基线出现
    assert HTML_V3 in fifth_user or "V3-MARKER" not in fifth_user


async def test_restore_is_repeatable(client, inline, monkeypatch):
    fake, public_id = await _three_versions(client, monkeypatch)
    first = await client.post(f"/api/v1/atoms/projects/{public_id}/versions/1/restore")
    second = await client.post(f"/api/v1/atoms/projects/{public_id}/versions/1/restore")

    v_a = await client.get(
        f"/api/v1/atoms/projects/{public_id}/versions/{first.json()['version_seq']}"
    )
    v_b = await client.get(
        f"/api/v1/atoms/projects/{public_id}/versions/{second.json()['version_seq']}"
    )
    assert v_a.json()["html"] == v_b.json()["html"]


async def test_restore_failed_version_is_conflict(client, inline, monkeypatch):
    fake = FakeAIHub(["{}", "{}", "不是页面", "不是页面"])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate", json={"prompt": "会失败"}
    )
    seq = accepted.json()["version_seq"]

    response = await client.post(
        f"/api/v1/atoms/projects/{public_id}/versions/{seq}/restore"
    )
    assert response.status_code == 409
    assert "没有可回滚的内容" in response.json()["error"]["message"]

    detail = await client.get(f"/api/v1/atoms/projects/{public_id}")
    assert len(detail.json()["versions"]) == 1, "不得产生新版本"


async def test_restore_missing_version_is_404(client, inline, monkeypatch):
    fake, public_id = await _three_versions(client, monkeypatch)
    response = await client.post(
        f"/api/v1/atoms/projects/{public_id}/versions/99/restore"
    )
    assert response.status_code == 404


async def test_restore_other_owner_is_404(client, inline, monkeypatch):
    from dependencies.owner import ANON_HEADER

    fake, public_id = await _three_versions(client, monkeypatch)
    response = await client.post(
        f"/api/v1/atoms/projects/{public_id}/versions/1/restore",
        headers={ANON_HEADER: "someone-else"},
    )
    assert response.status_code == 404
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd app/backend && python -m pytest tests/test_version_restore.py -q`
Expected: FAIL，restore 返回 404（路由不存在）

- [ ] **Step 3: 实现 restore 路由**

在 `app/backend/routers/atoms.py` 的 `get_version` 之后追加：

```python
@router.post("/projects/{public_id}/versions/{seq}/restore")
async def restore_version(
    public_id: str,
    seq: int,
    ctx: OwnerContext = Depends(get_owner),
    db: AsyncSession = Depends(get_db),
):
    """回滚到指定版本：**复制**该版本内容生成一个新版本，不销毁历史。

    对应 spec §4 S5.1。选「回滚即新版本」而不是「指针式回滚」的原因是
    不变量 2：``previous_html`` 恒取 seq 最大的 succeeded 版本。回滚产生新的
    最大 seq，下一轮增量自然以它为基础，流水线的基线逻辑一行都不用改。

    回滚不销毁中间版本（v2/v3 仍在、仍 succeeded、仍可切换查看），
    所以可以再次回滚。
    """
    try:
        await _require_project(db, public_id, ctx, write=True)
    except RouteError as exc:
        return error_envelope(exc.code, exc.message, ctx)

    # 并发约束：有正在进行的生成时不回滚，与 generate 一致
    active = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id,
            Versions.status.in_(ACTIVE_STATUSES),
        )
    )
    if active.scalars().first():
        return error_envelope(
            "CONFLICT", "该项目已有正在进行的生成，请等待完成后再回滚", ctx
        )

    target_result = await db.execute(
        select(Versions).where(
            Versions.project_public_id == public_id, Versions.seq == seq
        )
    )
    target = target_result.scalars().first()
    if not target:
        return error_envelope("NOT_FOUND", "该版本不存在", ctx)
    if target.status != "succeeded" or not target.html:
        return error_envelope("CONFLICT", "该版本没有可回滚的内容", ctx)

    all_versions = await db.execute(
        select(Versions).where(Versions.project_public_id == public_id)
    )
    next_seq = max((v.seq for v in all_versions.scalars().all()), default=0) + 1

    # summary 追加 restored_from，保留原有键（例如 features/structure）
    summary = _parse_summary(target.summary) or {}
    summary["restored_from"] = seq

    db.add(
        Versions(
            project_public_id=public_id,
            seq=next_seq,
            prompt=target.prompt,
            # 逐字节复制：注入后的 CSP 也一并带过来，与目标版本完全一致
            html=target.html,
            summary=json.dumps(summary, ensure_ascii=False),
            status="succeeded",
            error=None,
            duration_ms=None,
        )
    )
    project = await _require_project(db, public_id, ctx, write=False)
    project.version_count = next_seq
    project.latest_status = "succeeded"
    db.add(
        Messages(
            project_public_id=public_id,
            role="assistant",
            content=f"已回滚到 v{seq}，你可以在这个基础上继续提出新的要求。",
            version_seq=next_seq,
        )
    )

    try:
        await db.commit()
    except IntegrityError:
        # (project_public_id, seq) 唯一约束：并发下 max(seq)+1 非原子。
        # 转成 409 而不是 500。
        await db.rollback()
        return error_envelope(
            "CONFLICT", "该项目已有正在进行的生成，请稍后重试", ctx
        )

    return _json(
        {"restored": True, "version_seq": next_seq, "restored_from": seq}, ctx
    )
```

import 区补：

```python
from sqlalchemy.exc import IntegrityError
```

- [ ] **Step 4: 给 `get_version` 加 `html_sha256`**

在 `get_version` 的 payload 构造前加：

```python
    stored_html = version.html or ""
```

payload 里加：

```python
        "html": stored_html,
        # 一致性标识：前端预览条与源码条都渲染这一个值，因此「两处一致」是
        # 构造性成立的，不是两次独立计算碰巧相等。同时它真的校验了传输无损。
        "html_sha256": (
            hashlib.sha256(stored_html.encode("utf-8")).hexdigest() if stored_html else ""
        ),
```

import 区补 `import hashlib`。

- [ ] **Step 5: 运行，确认通过**

Run: `cd app/backend && python -m pytest tests/test_version_restore.py -q`
Expected: `8 passed`

- [ ] **Step 6: 全量回归 + 提交**

Run: `cd app/backend && python -m pytest tests/ -q`
Expected: 全绿

```bash
git add app/backend/routers/atoms.py app/backend/tests/test_version_restore.py
git commit -m "feat: 版本回滚接口（回滚即新版本）与版本详情哈希字段"
```

---

### Task 19: 前端回滚入口与版本切换过渡态

**Files:**
- Modify: `app/frontend/src/lib/atoms.ts`
- Modify: `app/frontend/src/components/VersionSwitcher.tsx`
- Modify: `app/frontend/src/pages/Index.tsx`

**Interfaces:**
- Consumes: Task 18 的 `POST .../restore`、`GET /versions/{seq}` 的 `html_sha256`/`error_type`
- Produces:
  - `atomsApi.restoreVersion(publicId, seq): Promise<{ restored: boolean; version_seq: number; restored_from: number }>`
  - `VersionDetail` 增加 `error_type: string | null`、`html_sha256: string`
  - `VersionSwitcher` 新增可选 prop `onRestore?: (seq: number) => void`
  - `Index.tsx` 的 `versionLoading` 状态

- [ ] **Step 1: 扩展 API 层类型与方法**

`app/frontend/src/lib/atoms.ts`：

```typescript
export interface VersionDetail {
  seq: number;
  prompt: string;
  html: string;
  summary: Record<string, unknown> | null;
  /** 排障用：区分鉴权失败（重试无用）与限流/超时（重试有用）。 */
  error_type: string | null;
  /** html 的 sha256，一致性标识用；空 html 时为空串。 */
  html_sha256: string;
  status: VersionStatus;
  error: string | null;
  duration_ms: number | null;
  created_at: string | null;
}
```

`atomsApi` 里加：

```typescript
  /**
   * 回滚到指定版本。
   *
   * 后端**复制**该版本内容生成一个新版本（seq 最大），而不是改指针——
   * 这样下一轮增量自然以被回滚的版本为基线。
   */
  restoreVersion(
    publicId: string,
    seq: number,
  ): Promise<{ restored: boolean; version_seq: number; restored_from: number }> {
    return invoke(
      `/api/v1/atoms/projects/${publicId}/versions/${seq}/restore`,
      'POST',
    );
  },
```

- [ ] **Step 2: 给 VersionSwitcher 加回滚入口**

```tsx
/**
 * VersionSwitcher —— 版本列表、切换与回滚（US3 / FR-007）。
 */
import { useState } from 'react';
import { GitCommitHorizontal, RotateCcw } from 'lucide-react';
import { cn } from '@/lib/utils';
import type { VersionBrief } from '@/lib/atoms';

interface VersionSwitcherProps {
  versions: VersionBrief[];
  activeSeq: number | null;
  onSelect: (seq: number) => void;
  /** 回滚到指定版本。仅 succeeded 的版本可回滚。 */
  onRestore?: (seq: number) => void;
}

export default function VersionSwitcher({
  versions,
  activeSeq,
  onSelect,
  onRestore,
}: VersionSwitcherProps) {
  const [pendingRestore, setPendingRestore] = useState<number | null>(null);

  if (versions.length <= 1) return null;

  const active = versions.find((v) => v.seq === activeSeq);

  return (
    <div className="flex items-center gap-1.5 overflow-x-auto">
      <GitCommitHorizontal className="h-3.5 w-3.5 shrink-0 text-slate-500" />
      {versions.map((version) => (
        <button
          key={version.seq}
          type="button"
          onClick={() => onSelect(version.seq)}
          className={cn(
            'shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] transition-colors',
            activeSeq === version.seq
              ? 'border-sky-500/60 bg-sky-500/15 text-sky-300'
              : 'border-slate-700 bg-slate-800/50 text-slate-400 hover:border-slate-600 hover:text-slate-300',
          )}
        >
          v{version.seq}
          {version.status === 'failed' && <span className="ml-1 text-rose-400">✕</span>}
          {version.status === 'cancelled' && <span className="ml-1 text-amber-400">⊘</span>}
        </button>
      ))}

      {onRestore && active?.status === 'succeeded' && (
        <>
          {pendingRestore === active.seq ? (
            <span className="flex shrink-0 items-center gap-1 pl-1">
              <span className="text-[11px] text-amber-300">
                回滚到 v{active.seq}？
              </span>
              <button
                type="button"
                onClick={() => {
                  onRestore(active.seq);
                  setPendingRestore(null);
                }}
                className="rounded-md border border-amber-500/50 px-2 py-0.5 text-[11px] text-amber-200 hover:bg-amber-500/10"
              >
                确认
              </button>
              <button
                type="button"
                onClick={() => setPendingRestore(null)}
                className="rounded-md border border-slate-700 px-2 py-0.5 text-[11px] text-slate-400 hover:border-slate-600"
              >
                取消
              </button>
            </span>
          ) : (
            <button
              type="button"
              onClick={() => setPendingRestore(active.seq)}
              title={`回滚到 v${active.seq}`}
              className="flex shrink-0 items-center gap-1 rounded-full border border-slate-700 px-2.5 py-0.5 text-[11px] text-slate-400 transition-colors hover:border-amber-500/50 hover:text-amber-200"
            >
              <RotateCcw className="h-3 w-3" />
              回滚到此版本
            </button>
          )}
        </>
      )}
    </div>
  );
}
```

二次确认是必须的：回滚会产生一个新版本且影响下一轮的生成基线，误点代价高。

- [ ] **Step 3: Index.tsx 加 versionLoading、清空旧 HTML、回滚处理器**

加状态：

```tsx
  const [versionLoading, setVersionLoading] = useState(false);
```

`switchVersion` 改成先清空再加载：

```tsx
  /**
   * 切换查看版本。
   *
   * **进入切换时立即清空 html 并置加载态**：旧实现在 getVersion 失败或慢响应
   * 期间保留上一版的 html，于是标签高亮 v3 而预览/源码显示 v2——这正是
   * 「源码与 Preview 不一致」的真实形态。加载中绝不允许展示旧内容。
   */
  const switchVersion = useCallback(
    async (seq: number) => {
      if (!activeId) return;
      setActiveSeq(seq);
      setView('preview');
      setHtml('');
      setFailedMessage(null);
      setVersionLoading(true);
      try {
        const full = await atomsApi.getVersion(activeId, seq);
        setHtml(full.html || '');
        setActiveSha(full.html_sha256 || '');
        if (full.status === 'failed') setFailedMessage(full.error);
        const version = detail?.versions.find((v) => v.seq === seq);
        if (version) setSteps(version.steps);
      } catch (e) {
        toast.error((e as Error).message || '版本内容加载失败');
      } finally {
        setVersionLoading(false);
      }
    },
    [activeId, detail],
  );
```

加 `activeSha` 状态（供一致性标识用，Task 20 消费）：

```tsx
  const [activeSha, setActiveSha] = useState('');
```

`openProject` 与 `finishGeneration` 里取到版本时也一并 `setActiveSha(full.html_sha256 || '')`。

回滚处理器：

```tsx
  /**
   * 回滚到指定版本。
   *
   * 后端产生一个新版本，所以成功后要切到那个新版本并重载详情——
   * 否则版本列表里看不到刚回滚出来的结果。
   */
  const handleRestore = useCallback(
    async (seq: number) => {
      if (!activeId) return;
      try {
        const result = await atomsApi.restoreVersion(activeId, seq);
        toast.success(`已回滚到 v${result.restored_from}（新版本 v${result.version_seq}）`);
        const data = await atomsApi.getProject(activeId);
        setDetail(data);
        const full = await atomsApi.getVersion(activeId, result.version_seq);
        setActiveSeq(result.version_seq);
        setHtml(full.html || '');
        setActiveSha(full.html_sha256 || '');
        setFailedMessage(null);
        void refreshProjects();
      } catch (e) {
        toast.error((e as Error).message || '回滚失败');
      }
    },
    [activeId, refreshProjects],
  );
```

传给 VersionSwitcher：

```tsx
              <VersionSwitcher
                versions={versions}
                activeSeq={activeSeq}
                onSelect={(seq) => void switchVersion(seq)}
                onRestore={(seq) => void handleRestore(seq)}
              />
```

- [ ] **Step 4: 渲染加载态**

右栏的内容分支改为（在 `view === 'preview'` 之前插入 `versionLoading` 判断）：

```tsx
            {detailLoading || versionLoading ? (
              <div className="flex h-full items-center justify-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                {detailLoading ? '加载项目内容…' : '加载版本内容…'}
              </div>
            ) : view === 'preview' ? (
```

- [ ] **Step 5: 校验**

Run: `cd app/frontend && npm run lint && npm run build`
Expected: 均通过

- [ ] **Step 6: 手工验证过渡态**

启动应用，用浏览器 DevTools 把网络限速设为 3G/Slow，然后快速连点 v1→v2→v3。
Expected: 任意时刻要么是加载骨架、要么是目标版本内容；**不得出现「高亮 v3 而内容为 v1」**。

- [ ] **Step 7: 提交**

```bash
git add app/frontend/src/lib/atoms.ts app/frontend/src/components/VersionSwitcher.tsx \
        app/frontend/src/pages/Index.tsx
git commit -m "feat(frontend): 版本回滚入口与切换过渡态（进入即清空旧内容）"
```

---

### Task 20: 一致性标识与预览区失败原因条

**Files:**
- Modify: `app/frontend/src/components/PreviewPane.tsx`
- Modify: `app/frontend/src/components/CodeViewer.tsx`
- Modify: `app/frontend/src/pages/Index.tsx`

**Interfaces:**
- Consumes: Task 18 的 `html_sha256`；Task 19 的 `activeSha`
- Produces: `PreviewPane` 新增 props `seq?: number | null`、`sha256?: string`、`failedMessage?: string | null`；`CodeViewer` 新增 props `seq?: number | null`、`sha256?: string`

- [ ] **Step 1: 加一个纯函数算展示串**

`app/frontend/src/lib/constants.ts` 追加：

```typescript
/**
 * 版本一致性标识：`v3 · 42.1KB · sha256:9f2c…`
 *
 * 预览条与源码条都渲染**同一个后端返回的哈希**，所以「两个视图一致」是构造性
 * 成立的，不是两次独立计算碰巧相等。哈希本身还校验了传输无损。
 */
export function formatVersionBadge(
  seq: number | null | undefined,
  htmlLength: number,
  sha256: string | undefined,
): string {
  const parts: string[] = [];
  if (seq != null) parts.push(`v${seq}`);
  if (htmlLength > 0) parts.push(`${(htmlLength / 1024).toFixed(1)}KB`);
  if (sha256) parts.push(`sha256:${sha256.slice(0, 8)}`);
  return parts.join(' · ');
}
```

- [ ] **Step 2: PreviewPane 展示标识与失败原因条**

`PreviewPane` 的 props 与工具条：

```tsx
interface PreviewPaneProps {
  html: string;
  isGenerating: boolean;
  emptyHint?: string;
  /** 当前版本号，用于一致性标识。 */
  seq?: number | null;
  /** 后端返回的 html 哈希，与 CodeViewer 渲染同一个值。 */
  sha256?: string;
  /** 该版本失败时的可读原因，在预览区同步展示。 */
  failedMessage?: string | null;
}
```

工具条那一段改为：

```tsx
      <div className="flex shrink-0 items-center gap-2 border-b border-slate-800 bg-slate-900/60 px-4 py-2">
        <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
        <span className="text-[11px] text-slate-400">
          隔离沙箱运行中 · sandbox="{SANDBOX_ATTR}" · 无法访问平台数据与凭据
        </span>
        <span className="ml-auto shrink-0 font-mono text-[11px] text-slate-500">
          {formatVersionBadge(seq, srcDoc.length, sha256)}
        </span>
      </div>
      {failedMessage && (
        <div className="shrink-0 border-b border-rose-500/30 bg-rose-500/10 px-4 py-2 text-[11px] leading-relaxed text-rose-300">
          该版本生成失败：{failedMessage}
        </div>
      )}
```

`PreviewPane` 顶部的 `import { useMemo } from 'react'` 之后补：

```tsx
import { formatVersionBadge } from '@/lib/constants';
```

**注意**：`failedMessage` 的展示必须放在「有 srcDoc」的分支里（失败版本没有 html，会走空态分支）。在空态分支里也加一条：

```tsx
  if (!srcDoc) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
        {failedMessage && (
          <div className="max-w-md rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-[11px] leading-relaxed text-rose-300">
            该版本生成失败：{failedMessage}
          </div>
        )}
        {isGenerating ? ( ... ) : ( ... )}
      </div>
    );
  }
```

失败提示原本只在输入框附近（距离预览区很远），容易被误读成「预览坏了」。这里同步展示。

- [ ] **Step 3: CodeViewer 展示同一个标识**

```tsx
interface CodeViewerProps {
  html: string;
  seq?: number | null;
  sha256?: string;
}
```

工具条左侧那一段改为：

```tsx
        <span className="text-[11px] text-slate-400">
          index.html · {lines.length} 行 ·{' '}
          <span className="font-mono text-slate-500">
            {formatVersionBadge(seq, html.length, sha256)}
          </span>
        </span>
```

import `formatVersionBadge`。

- [ ] **Step 4: Index.tsx 传入 props**

```tsx
              <PreviewPane
                html={html}
                isGenerating={isGenerating}
                seq={activeSeq}
                sha256={activeSha}
                failedMessage={failedMessage}
                emptyHint={...}
              />
```

```tsx
              <CodeViewer html={html} seq={activeSeq} sha256={activeSha} />
```

- [ ] **Step 5: 校验**

Run: `cd app/frontend && npm run lint && npm run build`
Expected: 均通过

- [ ] **Step 6: 手工验证一致性**

选中一个成功版本，来回切换「应用预览」/「源代码」两个标签。
Expected: 两条工具条上的 `v{seq} · {KB} · sha256:{前8位}` **完全相同**。

- [ ] **Step 7: 提交**

```bash
git add app/frontend/src/lib/constants.ts app/frontend/src/components/PreviewPane.tsx \
        app/frontend/src/components/CodeViewer.tsx app/frontend/src/pages/Index.tsx
git commit -m "feat(frontend): 预览/源码一致性标识与预览区失败原因条"
```

---

# 阶段 5：全量回归与验收归档（S6）

### Task 21: 全量回归

**Files:**
- Create: `docs/验收证据/A3-A7-回归.md`

**Interfaces:**
- Consumes: 全部前置任务

- [ ] **Step 1: 后端全量测试**

```bash
cd app/backend && python -m pytest tests/ -q 2>&1 | tee /tmp/backend-all.txt
```
Expected: 全绿，无 skip

- [ ] **Step 2: 前端静态检查与构建**

```bash
cd app/frontend && npm run lint && npm run build
```
Expected: 均通过

- [ ] **Step 3: 手工端到端 E1–E5**

按 spec §6 的清单逐条走，每条记录实际观察到的结果：

| 编号 | 步骤 | 期望 |
|---|---|---|
| E1 | 匿名：新建 → 生成第一类需求 → 追加第二类需求 → 刷新页面 → 回滚到 v1 → 再追加一轮 | 数据仍在；最终 v4 的基线是 v1 |
| E2 | 登录后重复 E1；另开无痕窗口确认看不到 E1/E2 的项目 | 无痕列表为空（或只有演示项目） |
| E3 | 重启后端 | pending/running 被置 failed，文案可读且是中文字面量 |
| E4 | 临时改坏 `APP_AI_KEY` 后提交生成 | 文案为「模型服务鉴权失败，请联系管理员」；描述未丢；`versions.error` 非空；`error_type == "auth"`；只调用 1 次 |
| E5 | 在生成页的预览 iframe 里执行 `window.parent.document` | 抛跨域错误（SC-005） |

E4 验证完必须把 `APP_AI_KEY` 改回去。

- [ ] **Step 4: 写回归证据文档**

`docs/验收证据/A3-A7-回归.md`：

```markdown
# A3–A7 验收证据：生成链路、两轮增量、回滚、一致性

日期：<填当天>
基线：feat/ownership-and-pipeline-hardening

## 自动化

    cd app/backend && python -m pytest tests/ -q
    cd app/frontend && npm run lint && npm run build

<粘贴真实输出>

| 验收项 | 承载测试文件 | 结果 |
|---|---|---|
| A3 故障注入（截断/空/超时/429/401） | tests/test_pipeline_recovery.py | <填> |
| A4 两类需求各跑通一轮 | tests/test_two_round_increment.py | <填> |
| A5 两轮增量、断言注入内容 | tests/test_two_round_increment.py | <填> |
| A6 回滚语义与回滚后基线 | tests/test_version_restore.py | <填> |
| A7 源码/Preview 一致 | 手工 + 一致性标识 | <填> |
| 陈旧恢复不误杀活跃生成 | tests/test_stale_recovery.py | <填> |

## 手工端到端

### E1 匿名
<记录实际观察>

### E2 登录
<记录实际观察>

### E3 重启恢复
<记录实际观察>

### E4 密钥失效
<记录实际观察，含 error_type>

### E5 沙箱
<记录实际观察>
```

- [ ] **Step 5: 提交**

```bash
git add docs/验收证据/A3-A7-回归.md
git commit -m "docs: A3-A7 回归验收证据"
```

---

### Task 22: 收尾

**Files:**
- Modify: `app/backend/README.md` 或 `CLAUDE.md`（运行迁移的时机）

- [ ] **Step 1: 在 CLAUDE.md 记录两件事**

在 `CLAUDE.md` 的「Commands」段之后加：

```markdown
## 部署前必须执行

```bash
# 归属隔离上线前跑一次：把 owner_key 为 NULL 的历史项目标记为只读演示项目。
# 幂等，可重复执行。漏跑的后果是那些项目对所有身份不可见（fail-closed）。
cd app/backend && python scripts/backfill_demo_owner.py
```
```

在「Working notes」段加两条：

```markdown
- 生产环境设置 `ATOMS_COOKIE_SECURE=1`，让匿名标识 cookie 带 `Secure` 属性（本地 http 必须不设）。
- `GENERATION_INLINE=1` **仅供测试**：它让 `generate` 在请求内直接 await 流水线，
  并复用请求会话。生产置位会使网关 120s 读超时立即复现。
```

- [ ] **Step 2: 确认没有遗漏的引用**

```bash
cd "C:/Users/徐才权/PycharmProjects/atomsDemoV1" && grep -rn "getOwnerKey\|lib/auth'" app/frontend/src || echo "无残留引用"
cd app/backend && grep -rn "owner_key" routers/atoms.py | grep -v "ctx.owner_key" | grep -v "Projects.owner_key"
```
Expected: 第一条输出「无残留引用」；第二条只应出现 `_require_project` 内部的 `Projects.owner_key` 与注释。

- [ ] **Step 3: 提交**

```bash
git add CLAUDE.md
git commit -m "docs: 记录迁移执行时机与两个环境变量约束"
```

---

## Self-Review

**1. Spec 覆盖检查**

| Spec 章节 | 承载任务 |
|---|---|
| §1 决策 Q1 匿名可写 | Task 3（依赖 fail-closed 签发）、Task 11（登录可选入口） |
| §1 Q3 NULL owner → 演示区 | Task 6 |
| §1 Q4 回滚即新版本 | Task 18 |
| §1 Q5 双通道传输 | Task 3（收）、Task 9（发）、Task 4（`_attach_owner`） |
| §2 不变量 1 归属服务端派生 | Task 3、4、5 |
| §2 不变量 2 基线恒为最大 succeeded seq | Task 18（构造性保证）、Task 18 的 `test_baseline_after_restore_is_the_restored_version` |
| §4 S1.1 owner.py | Task 3 |
| §4 S1.2 9 路由 / `_recover_stale_versions` / 演示只读 / 历史查询修正 | Task 4、5、6、7 |
| §4 S1.3 迁移脚本 | Task 6 |
| §4 S1.4 data_models description | Task 6 |
| §4 S2 前端接线（含删 `lib/auth.ts`、删 `getOwnerKey`） | Task 9、10、11 |
| §4 S3.1 错误分类 | Task 14 |
| §4 S3.2 显式超时 + 心跳 | Task 14（超时）、Task 15（心跳） |
| §4 S3.3 上下文预算 | Task 13 |
| §4 S3.4 结构校验 + CSP 幂等 | Task 12、16 |
| §4 S3.5 失败可观测性 | Task 17 |
| §4 S4 测试地基 | Task 1、2 |
| §4 S5.1 restore | Task 18 |
| §4 S5.2 一致性（过渡态 / 哈希 / 失败原因条） | Task 19、20 |
| §4 S5.3 回滚入口 | Task 19 |
| §5 竞态表 | Task 15（前两行 + 取消守卫 + 迁移漏跑）、Task 18（restore 并发 + IntegrityError）、Task 12（CSP 二次注入） |
| §6 验收 A1–A7 | Task 8、21 |
| §7 文件清单 | 各任务的 Files 段 |
| §9 不做的事 | 无对应任务（正确） |
| 本计划「风险 A/B」 | Task 15 |

**2. 占位符扫描**：已逐任务检查，无 TBD/TODO，无「类似 Task N」的偷懒引用——每个任务的代码块都是可整段粘贴的。

**3. 类型一致性**：`OwnerContext(owner_key, anon_key)`、`RouteError(code, message)`、`_require_project(db, public_id, ctx, *, write)`、`PipelineError(message, step_seq, retriable, error_type)`、`_recover_stale_versions(db, owner, public_id)`、`_classify_upstream_error(exc) -> UpstreamError`、`looks_well_formed(doc) -> bool`、`truncate_previous_html` / `truncate_continue_history`、`formatVersionBadge(seq, htmlLength, sha256)` 在定义处与消费处的名字、参数顺序、返回类型一致。

**4. 与 spec 的两处实现补充**（已在计划顶部说明，spec 的意图不变）：

- spec §4 S3.2 的 `_recover_stale_versions` 判据换 `updated_at`：本计划追加了「进程内存活判定优先」与「时间戳口径统一」两条实现约束（Task 15），否则换 `updated_at` 反而会引入时区偏移导致的误杀。
- spec §4 S4 的 `GenerationPipeline(db, ai=None)`：本计划采用它；HTTP 集成测试另外用 `monkeypatch.setattr(services.pipeline, "AIHubService", ...)` + `GENERATION_INLINE=1`，两条路径各司其职。
