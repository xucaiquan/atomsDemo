# Atoms Studio：无登录数据隔离与生成链路加固 — 设计文档

**日期**：2026-09-20
**状态**：已与业主确认，可进入实施计划
**来源**：`docs/优化文档-无登录数据隔离与生成链路修复.md`（现状审计 + 施工单）
**代码基线**：`app/`，分支 `main`，commit `a4295e8`

本文档是该优化的**权威施工口径**。凡与来源文档冲突之处，以本文档末尾「§8 与来源文档的偏离」为准。

---

## 1. 决策记录

来源文档 §8.1 的四个问题已于 2026-09-20 确认：

| 编号 | 决策 | 影响 |
|---|---|---|
| Q1 | **匿名可写 + 登录可选** | 保留「无需注册即可用」（FR-014 / T062）；隔离由服务端派生身份保证，不靠登录 |
| Q2 | 匿名与登录**不合并**数据 | 两套身份各自持有列表；登录后看不到匿名项目，UI 需明示 |
| Q3 | NULL owner 历史数据 **标为只读演示项目** | 迁移动作为 `is_demo := true`；判据统一用 `is_demo` |
| Q4 | 回滚采用 **「回滚即新版本」** | 不新增数据模型列；基线选择天然正确 |
| Q5 | 匿名标识传输 **cookie + 请求头双通道** | 消除「线上网关吃掉 Set-Cookie 就静默失效」的单点 |

**交付范围**：全量 S1–S6。

---

## 2. 两条不变量

本优化的全部改动都在维护这两条不变量。任何后续修改若破坏其中之一，即为回归。

### 不变量 1 — 归属由服务端派生，请求体永不参与

`projects` 域的**每一次**读写都必须经过唯一过滤入口。归属键的来源优先级：

```
登录 JWT 的 sub  >  签名 cookie  >  X-Atoms-Anon 请求头  >  服务端新签发
```

客户端提供的任何字段（含历史遗留的 `owner_key`）**不得**影响归属。查不到归属一律 **404**（fail-closed），不用 403——避免探测项目是否存在。

### 不变量 2 — 多轮增量的基线永远是「seq 最大的 succeeded 版本」

`routers/atoms.py` 的 `previous_html` 查询不按「当前查看的版本」取，而按 `seq desc` 取最新成功版本。因此：

- **回滚必须产生新的最大 seq**，否则用户回滚到 v2 后再提需求，系统会拿 v5 当基线，回滚形同虚设（来源文档 §5.1 指出的设计缺陷）。这是 Q4 选「新版本」的根本原因。
- 回滚**不修改流水线的基线逻辑**——新 seq 自然成为最大值。

---

## 3. 整体数据流

```
                    ┌──────────────────────── dependencies/owner.py ───────────────────────┐
HTTP 请求 ─────────>│ get_owner(request, credentials) -> OwnerContext                        │
                    │   ① JWT sub  → "user:<sub>"                                            │
                    │   ② cookie atoms_anon（HMAC 验签）→ "anon:<nonce>.<sig>"               │
                    │   ③ X-Atoms-Anon 头（HMAC 验签）→ 同上                                 │
                    │   ④ 均无效 → 新签发，随响应回传                                        │
                    └───────────────────────────────┬────────────────────────────────────────┘
                                                    │ OwnerContext(owner_key, anon_key)
                                                    v
                    ┌──────────────────────── routers/atoms.py ──────────────────────────────┐
                    │  _visible(stmt, owner)   读路径：owner 匹配 OR is_demo                 │
                    │  _owned(stmt, owner)     写路径：owner 匹配                            │
                    │  _require_project(db, pid, ctx, write=) -> Projects | raise RouteError │
                    │  RouteError.code/message ──> error_envelope(code, message, ctx)        │
                    └───────────────────────────────┬────────────────────────────────────────┘
                                                    v
                    响应统一经 _json(payload, ctx, status) 构造：
                      · Set-Cookie: atoms_anon=<nonce>.<sig>; HttpOnly; SameSite=Lax; Max-Age=180d
                      · GET /projects 的响应体额外携带 anon_key（前端持久化，作为请求头回传）
```

匿名标识格式与长度约束（`projects.owner_key` 模型上限 64 字符）：

```
nonce = secrets.token_hex(16)            → 32 字符
sig   = HMAC-SHA256(jwt_secret_key, nonce).hexdigest()[:16]  → 16 字符
raw   = f"{nonce}.{sig}"                 → 49 字符
owner_key = f"anon:{raw}"                → 54 字符  ✓
owner_key = f"user:{sub}"                → 5 + 36 = 41 字符（sub 为 UUID）✓
```

> **2026-09-20 修订（T3 执行期，见账本 Ruling 13）**：上面 `user:{sub}` 的 41 字符算式
> 基于「`sub` 是 UUID」这一前提，而本平台不成立——`models/auth.py:12` 的 `users.id` 是
> `String(255)`（注释「Use platform sub as primary key」），`services/auth.py:19,40` 把
> OIDC 的 `sub` claim 原样存进去；而 `models/projects.py:15` 的 `owner_key` 只有
> `String(64)`。`sub` 超过 59 字符即导致插入失败。**实际实现改为
> `owner_key = f"user:{sha256(sub).hexdigest()[:32]}"`（37 字符）**：单格式、定宽、
> 无静默碰撞（朴素截断会让共享前缀的两个 sub 并成同一身份）。`anon:` 分支不受影响。
> 本文件其余部分不再改动；下游 T6 的 `owner_key` 字段 description 需同步为「用户 ID 的
> 哈希」而非字面 ID。

---

## 4. 单元设计

### S1 — 归属隔离

#### S1.1 新增 `app/backend/dependencies/owner.py`

```python
ANON_COOKIE = "atoms_anon"
ANON_HEADER = "X-Atoms-Anon"
ANON_MAX_AGE = 180 * 24 * 3600          # 180 天

@dataclass(frozen=True)
class OwnerContext:
    owner_key: str                # "user:<sub>" | "anon:<nonce>.<sig>"
    anon_key: str | None          # 匿名身份时为 raw；登录身份时为 None
```

- `_sign(nonce) -> str`：`hmac.new(secret, nonce.encode(), sha256).hexdigest()[:16]`，secret 取 `settings.jwt_secret_key`。
- `_verify(raw) -> str | None`：拆 `raw.split(".")`，重算签名后 `hmac.compare_digest` 比对；通过则返回 nonce，否则 None。
- `_issue() -> str`：`f"{secrets.token_hex(16)}.{_sign(nonce)}"`。
- `async def get_owner(request, credentials=Depends(bearer_scheme)) -> OwnerContext`：按 §2 不变量 1 的优先级解析。JWT 解析失败（`AccessTokenError`）**不报错**，落到匿名分支——密钥未配置的部署仍能按匿名身份工作。

**签发策略**：所有 atoms 路由共用这一个依赖；凡解析不出有效标识的请求，一律视为新匿名会话并签发（幂等）。不采用来源文档「只在 `list_projects`/`create_project` 下发」的写法，理由见 §8 偏离 1。

#### S1.2 `routers/atoms.py` 改造

新增唯一过滤入口与错误类型：

```python
class RouteError(Exception):
    def __init__(self, code: str, message: str) -> None: ...

def _visible(stmt, owner: str):     # 读路径：本人的 + 演示项目
    return stmt.where(or_(Projects.owner_key == owner, Projects.is_demo.is_(True)))

def _owned(stmt, owner: str):       # 写路径：仅本人的
    return stmt.where(Projects.owner_key == owner)

async def _require_project(db, public_id, ctx, *, write: bool) -> Projects:
    """查不到 → RouteError(NOT_FOUND)；write 且命中演示项目 → RouteError(CONFLICT)。"""
```

`_require_project` 是本模块**唯一**的项目获取入口，8 条路由不得自行拼 `where`。

响应构造统一经：

```python
def _json(payload, ctx, status_code=200) -> JSONResponse:
    """构造响应并附加匿名 cookie（ctx.anon_key 非空时）。"""

def error_envelope(code, message, ctx=None) -> JSONResponse:   # 保留原签名，新增可选 ctx
```

现有 8 条路由逐一改造，另新增第 9 条：

| # | 路由 | 改法 |
|---|---|---|
| 1 | `GET /projects` | `_visible(select(Projects), ctx.owner_key).order_by(updated_at.desc())`；响应体加 `anon_key`；`_recover_stale_versions(db, ctx.owner_key)` |
| 2 | `POST /projects` | `owner_key=ctx.owner_key`；删除请求体 `owner_key` 字段 |
| 3 | `GET /projects/{pid}` | `_require_project(db, pid, ctx, write=False)` |
| 4 | `GET /projects/{pid}/versions/{seq}` | 先 `_require_project`；响应体加 `html_sha256` |
| 5 | `DELETE /projects/{pid}` | `_require_project(db, pid, ctx, write=True)` |
| 6 | `POST /projects/{pid}/generate` | `_require_project(db, pid, ctx, write=True)`；删除请求体 `owner_key` 字段 |
| 7 | `GET /projects/{pid}/versions/{seq}/steps` | 先 `_require_project` |
| 8 | `POST /projects/{pid}/versions/{seq}/cancel` | 先 `_require_project` |
| 9 | `POST /projects/{pid}/versions/{seq}/restore`（新增，见 S5） | 先 `_require_project(write=True)` |

**`_recover_stale_versions` 两项改动**：

1. 签名改 `(db, owner: str, public_id: str | None = None)`，查询 JOIN `Projects` 限定 `owner_key == owner`——消除「任意访客一次列表请求触发全表扫描 + 全表 UPDATE」这一放大攻击面。
2. 判据从 `created_at` 改为 `updated_at`（配合 S3 心跳），见 §5 竞态分析。

**演示项目只读**：写路径命中 `is_demo=true` 返回 409 CONFLICT，文案「这是演示项目，仅供浏览；请点左上角「新项目」创建你自己的项目」。不用 404——演示项目在列表里本来就可见，用 404 会让用户以为数据坏了。

**`generate` 的历史查询修正**（来源文档 §4.1 缺陷 1）：`select(Versions.prompt)` 增加 `where(Versions.status == "succeeded")`，避免失败/取消轮次的需求污染指代消解。**必须仍在 `prepare()` 落库新版本之前查询**（现有注释已强调，改造时保留）。

#### S1.3 数据迁移（必须执行）

新增 `app/backend/scripts/backfill_demo_owner.py`：

```sql
UPDATE projects SET is_demo = true WHERE owner_key IS NULL;
```

- 判据统一为 `is_demo`，**不用** `owner_key IS NULL`（理由见 §8 偏离 4）。
- 脚本幂等，可重复执行。
- 未执行脚本的后果是那些项目对所有身份不可见（fail-closed 安全侧），**不是**人人可见。
- `list_projects` 中增加一条自检：若 `owner_key IS NULL AND is_demo = false` 的计数 > 0，打 `logger.error` 提示迁移未执行。只记日志，不改变行为。

#### S1.4 `data_models/projects.json`

仅更新 `owner_key` 的 `description`：说明取值形如 `user:{sub}` / `anon:{nonce}.{sig}`，由服务端派生，不接受客户端传入。**不改 `maxLength`、不改字段名**（`models/**` 由 schema 生成，改 schema 会触发重新生成，风险大于收益）。

### S2 — 前端接线

| 文件 | 改动 |
|---|---|
| `contexts/AuthContext.tsx` | **改写**为基于 `client.auth.me()/toLogin()/logout()` 的实现，保留现有 `useAuth()` 导出签名（`user/loading/error/login/logout/refetch/isAdmin`）——三态：`loading` / `authenticated` / `anonymous` |
| `App.tsx` | `AuthProvider` 挂到 `MODULE_PROVIDERS_START` 槽位 |
| `lib/auth.ts` | **删除**（依赖后端并不返回的 `redirect_url`，且唯一消费者已被改写） |
| `components/ProtectedAdminRoute.tsx` | 不动。全局无引用；改写 `AuthContext` 后其行为反而正确 |
| `lib/constants.ts` | 删除 `getOwnerKey()`（全仓无调用点，且是客户端可伪造的标识） |
| `lib/atoms.ts` | `invoke()` 统一附加 `X-Atoms-Anon` 头（取自 localStorage）；`listProjects()` 把响应里的 `anon_key` 写回 localStorage；新增 `restoreVersion()`；`VersionDetail` 加 `error_type`、`html_sha256` |
| `pages/Index.tsx` | ① 顶栏加登录/登出入口（仅 `anonymous` 显示「登录」）；② 登录/登出成功后清空 `projects`/`detail`/`html`/`activeId` 并重载；③ `switchVersion` 进入时 `setHtml('')` + `versionLoading` 态；④ 回滚入口；⑤ 预览区失败原因条 |

**禁止**：`if (!user) navigate('/auth/callback')`（OIDC 回调参数缺失会无限循环）。未登录就是正常可用状态，登录是可选升级。

### S3 — 生成链路稳定性

#### S3.1 错误分类

`services/aihub_errors.py` 新增：

```python
class UpstreamError(Exception):
    """上游模型服务的可分类故障。"""
    kind: str            # "auth" | "rate_limit" | "timeout" | "upstream_5xx" | "unknown"
    status_code: int | None
    retriable: bool
```

`pipeline.py` 新增 `_classify_upstream_error(exc) -> UpstreamError`，把 openai SDK 异常（`AuthenticationError` / `PermissionDeniedError` / `RateLimitError` / `APITimeoutError` / `APIConnectionError` / `InternalServerError`）与 `asyncio.TimeoutError` 映射为上述 kind。映射失败时归 `unknown` 且 `retriable=True`（宁可多试一次）。

| 上游症状 | kind | 策略 | 用户文案 |
|---|---|---|---|
| 401 / 403 | `auth` | **不重试**，立即失败 + ERROR 日志 | 「模型服务鉴权失败，请联系管理员」 |
| 429 | `rate_limit` | 退避重试 0.5→1→2s，最多 3 次 | —（用户无感） |
| 超时 / 连接错误 / 5xx | `timeout` / `upstream_5xx` | 同上 | —（用户无感） |
| 空内容 `content == ""` | `empty` | 现有单次重试 **+ 降 `max_tokens` 再试一次** | 「模型返回了空内容，请重新提交生成」 |
| 无 `</html>` | `truncated` | 走既有续写/重跑链 | 「生成的页面内容不完整（可能被截断），请简化需求后重试」 |

`PipelineError` 增加可选 `retriable: bool = False` 与 `error_type: str | None = None`，保留 `(message, step_seq)` 位置参数兼容。

#### S3.2 显式超时 + 心跳

**超时**：`_call_step` 用 `asyncio.wait_for(self._ai.gentxt(request), timeout=STAGE_TIMEOUT)` 包裹，`STAGE_TIMEOUT = 240.0`。超时映射为 `kind="timeout"` 且可重试。

> 为什么不改 `services/aihub.py` 的 `AsyncOpenAI(timeout=…)`：该文件是平台通用层，`routers/aihub.py` 等亦共用，改它会把行为变更外溢到本项目之外。`wait_for` 效果等价且零外溢。见 §8 偏离 3。

**心跳**（解来源文档 §3.1 问题 3 的误杀竞态）：`run()` 启动一个独立的 asyncio 任务，每 30s 用**自己的** DB 短事务把 `versions.updated_at` 置为当前时间。

```python
async def _heartbeat(self, public_id: str, version_seq: int) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)          # 30.0
        async with db_manager.session() as session:      # 独立会话，不复用 self._db
            # 仅在版本仍处于 ACTIVE_STATUSES 时刷新
            ...
```

- **必须用独立会话**：SQLAlchemy `AsyncSession` 禁止被两个协程并发使用，而心跳在 AI 调用期间持续运行。见 §8 偏离 2。
- `run()` 用 `try/finally` 保证心跳任务被取消，避免孤儿任务。
- `_recover_stale_versions` 的判据相应改为 `updated_at`。三者关系（写进代码注释与 `test_stale_recovery.py`）：
  `HEARTBEAT_INTERVAL(30s) < STALE_AFTER(10min) < GENERATION_POLL_TIMEOUT_MS(12min)`
- 收益：长任务不被误判，且无需为了「防误杀」而放大 `STALE_AFTER`（放大反而让真挂死的任务卡更久）。
- **实施前须确认** `models/versions.py` 存在 `updated_at` 列；若不存在则退化为心跳写入 `versions.summary` 的时间戳子键，并在测试中断言。

#### S3.3 上下文预算

`services/prompts.py` 新增：

```python
PREVIOUS_HTML_MAX_CHARS = 24_000      # 阶段 1/2/3 注入上一版 HTML 的上限
CONTINUE_HISTORY_MAX_CHARS = 12_000   # 续写调用 history 中回传 doc 的上限（只回传尾部）

def truncate_previous_html(html: str) -> str: ...
def truncate_continue_history(doc: str) -> str: ...
```

- `truncate_previous_html`：未超限原样返回；超限则保留 `<head>` 段 + 前 N 字符 + 末尾 2000 字符，中间插入 `<!-- …已省略 N 字符… -->` 标记。
- `truncate_continue_history`：取 `doc[-CONTINUE_HISTORY_MAX_CHARS:]`，前置一行说明「以下是已输出内容的尾部片段」。`CONTINUE_HISTORY_MAX_CHARS > CONTINUE_TAIL_CHARS(2000)` 是硬要求，否则续写点上下文会被裁掉。
- `build_code_user` 注入 `previous_html` 前必须过闸门；`_generate_code` 续写调用的 `history[1].content` 改为 `truncate_continue_history(doc)`。
- `HISTORY_ITEM_CHARS` 200 → **400**，且**最新一条放宽到 600 字符**（来源文档 §4.1 缺陷 2：长需求 + 指代时 200 字会丢掉「不要用 CDN」这类关键约束）。定义：「最新一条」= `build_history_block` 中 `cleaned[-HISTORY_MAX_ITEMS:]` 切片的**最后一个元素**，即时间上最靠后、也是当轮指代最可能指向的那条需求。总预算仍为 `5 × 400 + 600 = 2600` 字符量级。

#### S3.4 结构完整性校验

`services/html_extract.py` 新增纯函数：

```python
def looks_well_formed(doc: str | None) -> bool:
    """① 以 </html> 结尾 ② <!DOCTYPE 至多 1 个 ③ <html 恰好 1 个 ④ <body 恰好 1 个。"""
```

用编译好的正则计数（`<html[\s>]` / `<body[\s>]`），避免 `</html>`、`</body>` 被误计入。

判定升级：`_generate_code` 的「是否成功」从 `is_complete_document(doc)` 升级为 `is_complete_document(doc) and looks_well_formed(doc)`。这样「模型续写时重开文档」不再依赖开头的 `_DOC_RESTART_PATTERN.match`——若 `_merge_continuation` 拼接后出现两个 `<!DOCTYPE` 或两个 `<body`，直接判定为「重开文档」并采用新产出。正则 `match` 只匹配开头，模型在开头多输出一句解释就会漏判，导致两份文档被拼在一起却因末尾恰好是 `</html>` 而入库（脏数据）。

同时确认 `inject_csp` 幂等（已存在 CSP 时不重复注入），补单元测试。**双 CSP meta 的风险**：第二个 `default-src 'none'` 会覆盖更宽松的策略。

#### S3.5 失败可观测性

- `versions.summary`（JSON 列已存在，**不改模型**）增加子键：
  `{"error_type": "auth|timeout|rate_limit|truncated|empty|upstream_5xx|unknown", "upstream_status": 429, "attempts": 3}`
- `GET /versions/{seq}` 顶层增加便捷字段 `error_type`（从 `summary` 派生）。
- 日志统一格式：`project=<前8位> seq=<n> step=<n> model=<m> attempt=<n> duration_ms=<n>`，用于对齐「1~4 分钟」预期与定位慢阶段。

### S4 — 测试地基

**这是 A3/A4/A5 能否自动化的前提。**

| 文件 | 内容 |
|---|---|
| `tests/conftest.py`（新增） | `pytest-asyncio` 配置 + `httpx.AsyncClient(transport=ASGITransport(app))` + SQLite 内存库 + `FakeAIHub` fixture + `override get_db` |
| `tests/fakes.py`（新增） | `FakeAIHub`：队列化响应，并**记录每一次 `GenTxtRequest`** |

`FakeAIHub` 契约：

```python
class FakeAIHub:
    """脚本化上游。script 元素可为：str（作为 content）、Exception（抛出）、
    Callable[[GenTxtRequest], str | Exception]（按请求分支）。"""
    def __init__(self, script: list[Any]) -> None: ...
    async def gentxt(self, request: GenTxtRequest) -> GenTxtResponse: ...
    requests: list[GenTxtRequest]    # 断言注入内容的唯一可靠来源
```

生产代码的两处可测性改造：

1. `GenerationPipeline.__init__(self, db: AsyncSession, ai: AIHubService | None = None)` — 默认 `AIHubService()`，测试注入 `FakeAIHub`。无需真实 key、不花配额。
2. `GENERATION_INLINE` 环境变量开关：`_spawn_generation` 在 `os.getenv("GENERATION_INLINE")` 为真时**直接 `await` 流水线**而非 `asyncio.create_task`。注释写明**仅测试用**。理由：轮询后台任务到终态会引入 flaky 与慢测试。

数据库：`sqlite+aiosqlite://` + `StaticPool`（内存库每连接独立，必须固定单连接）。**实施风险**：`models/**` 生成的列含 `DateTime(timezone=True)`，能否直接建表未实测。S4 开头先做这一项 spike；若不通过，退路是 `sqlite+aiosqlite:///file:test?mode=memory&cache=shared&uri=true` 或临时文件库。

新增测试文件：

```
tests/test_owner_isolation.py      A1/A2  8 条路由越权矩阵 + cookie/头伪造 + 删 cookie 后 404 + 演示项目只读
tests/test_pipeline_recovery.py    A3     截断→续写成功 / 空内容×3 / 429 重试后成功 / 401 零重试
tests/test_two_round_increment.py  A4/A5  两类需求 + 两轮增量，断言 FakeAIHub 收到的 messages 文本
tests/test_version_restore.py      A6     回滚语义与「回滚后基线正确」
tests/test_stale_recovery.py       心跳 vs created_at 误杀回归
```

现有 `tests/test_html_extract.py` 增补：CSP 幂等、`looks_well_formed` 的结构用例。
现有 `tests/test_context_memory.py` 需随 `HISTORY_ITEM_CHARS` 变更调整断言。

测试仍必须 `cd app/backend && python -m pytest tests/ -q`（无 `conftest.py` 时依赖 `python -m` 把 CWD 放进 `sys.path`；新增 `conftest.py` 后此约束依然保留）。

### S5 — 回滚与源码/Preview 一致性

#### S5.1 回滚接口

```
POST /api/v1/atoms/projects/{public_id}/versions/{seq}/restore
```

- 前置：`_require_project(write=True)`；目标版本存在、`status == "succeeded"`、`html` 非空，否则 409「该版本没有可回滚的内容」。
- 并发：存在 `pending`/`running` 版本时 409（复用现有检查）。
- 行为：创建 `seq' = max(seq) + 1` 的新版本，`html`/`prompt` 复制自目标版本（**逐字节**），`summary` 追加 `{"restored_from": seq}`，`status="succeeded"`，`duration_ms=None`；写一条 assistant message「已回滚到 v{seq}」；更新 `project.version_count` 与 `latest_status`。
- 返回：`{"restored": true, "version_seq": seq', "restored_from": seq}`
- **不销毁历史**：v3/v4/v5 仍在，可再次回滚。
- **基线自动正确**：新 seq 即最大 seq，下一轮增量自然以它为基线，无需改流水线。

**`next_seq` 竞态**：`pipeline.py` 的 `max(seq) + 1` 非原子。restore 与 generate 共用同一并发检查（实质串行化），并**捕获 `IntegrityError` 转 409**，防止 `(project_public_id, seq)` 唯一约束冲突变成 500。

#### S5.2 一致性修复

问题在于**过渡态**，而非两份数据来源——两视图本就共用一个 `html` 状态（`Index.tsx:64`），`PreviewPane` 用 `srcDoc={html}`，`CodeViewer` 用 `html.split('\n')`。

1. **切换版本失败时保留旧内容**（`Index.tsx:117-134`）：`setActiveSeq(seq)` 立即高亮新版本，但 `getVersion` 失败时 `html` 仍是上一个版本 → 「高亮 v3 而预览/源码是 v2」。修法：进入切换即 `setHtml('')` 并置 `versionLoading`，渲染骨架/错误态，**不允许在加载中展示旧 HTML**。
2. **无「这是哪一版」的可核验标识**：后端 `GET /versions/{seq}` 返回 `html_sha256`（`hashlib.sha256(html.encode()).hexdigest()`）；预览工具条与源码工具条都渲染**同一个响应里的这个值**：`v{seq} · {KB} · sha256:{前8位}`。
   - 用后端返回值而非前端自算（WebCrypto/djb2）：两处渲染同一个值，「一致」是构造性成立而非两次独立计算碰巧相等；且哈希确实校验了传输无损。见 §8 偏离 5。
   - `html` 为空时（失败版本）不显示哈希。
3. **失败版本的空 HTML 与提示区距离远**：`failedMessage` 目前只在输入框附近展示（`Index.tsx:440-444`），预览区看起来像「预览坏了」。修法：预览区同步展示失败原因条。

#### S5.3 前端回滚入口

`VersionSwitcher` 每项加「回滚到此版本」（二次确认对话框），成功后 `setActiveSeq(新 seq)`、重载详情与 HTML、`toast.success`。仅在目标版本 `succeeded` 时可用。

---

## 5. 竞态与边界分析

| 场景 | 分析 | 保护 |
|---|---|---|
| 生成中任意访客打开列表 → 误把 running 版本置 failed | 流水线上限 = 3 次模型调用 × SDK 默认超时，可能 > 10 分钟；原判据用 `created_at`（受理时刻），必然误杀 | 心跳刷 `updated_at` + 判据改 `updated_at`（S3.2） |
| 心跳与 pipeline 共用 `AsyncSession` | SQLAlchemy 禁止并发使用同一 session | 心跳用自己的 `db_manager.session()`（S3.2） |
| 取消后迟到的心跳写入 | 心跳只在版本仍属 `ACTIVE_STATUSES` 时刷新 | 与既有取消守卫一致 |
| 取消后迟到的成功结果 | 既有守卫 `if version.status in ACTIVE_STATUSES` | 保留不动 |
| restore 与 generate 并发 | `max(seq)+1` 非原子 | 共用并发检查 + `IntegrityError` → 409（S5.1） |
| 匿名 cookie 被网关吃掉 | 每次请求都是新身份 → 建项目后立刻 404 | 双通道：响应体带 `anon_key`，前端持久化后走 `X-Atoms-Anon` 头（§1 Q5） |
| 伪造 `X-Atoms-Anon` / cookie | 标识是 HMAC 签名的，客户端无法构造有效签名 | `_verify` + `compare_digest`（S1.1） |
| 迁移脚本漏跑 | NULL owner 项目对所有人不可见 | fail-closed（安全侧）；`list_projects` 自检打 ERROR 日志 |
| `inject_csp` 二次注入 | 两个 CSP meta，第二个 `default-src 'none'` 覆盖宽松策略 | 幂等 + 单测（S3.4）；回滚路径复用同一函数 |
| `_RUNNING_TASKS` 进程内 | 多 worker 部署取消失效 | 已记录于 CLAUDE.md，本次不改；单 worker 部署 |
| 共享的 SQLite 内存库 | 内存库每连接独立，测试会看不到表 | `StaticPool`（S4） |

---

## 6. 验收矩阵

每条验收项必须附**可观测证据**，不给证据不算通过。测试脚本与命令见 S4 与来源文档 §2.2⑤。

| 编号 | 验收项 | 证据 |
|---|---|---|
| A1 | 无登录 / 有登录两种身份的数据隔离 | 双会话脚本：A 建的 3 个项目在 B 的 `GET /projects` 中出现 **0** 次；B 直接 `GET /projects/{A的id}` → **404** |
| A2 | 归属过滤覆盖全部读写路径 | 9 条路由的越权矩阵全绿（生成、删除、取消、回滚、步骤、版本均 404）；伪造 `owner_key` 后归属仍是请求者；删除 `atoms_anon` cookie 且不带请求头后访问既有项目 → 404；演示项目写操作 → 409 |
| A3 | 模型代码生成服务可恢复 | 注入「截断 / 空内容 / 超时 / 429 / 401」五类故障，版本终态与调用次数符合 §4 S3.1 表；401 只调用 1 次 |
| A4 | 两类需求各跑通一轮 | 新需求（从零）与迭代需求（指代消解）各产出 1 个 succeeded 版本 |
| A5 | 同项目连续两轮增量 | 两轮 succeeded；断言 `FakeAIHub` 收到的 messages 含「需求历史」且含第一轮需求特征串、含「上一版页面」、历史条数 == 1（失败轮次不入历史）、prompt 总字符数 ≤ 预算 |
| A6 | 版本回滚 | v3.html 与 v1.html **逐字节相等**；v2 仍存在且可切换；回滚后一轮的 `previous_html` **等于 v3.html**（基线正确）；对 `failed` 版本回滚 → 409 且无新版本 |
| A7 | 源码 / Preview 一致 | 预览条 == 源码条 == `GET /versions/{seq}` 的 `html_sha256` 前 8 位；网络节流 3s 下快速切换 v1→v2→v3 不出现「高亮 v3 而内容是 v1」 |

手工端到端（每次改完 pipeline 或归属后跑一遍，见来源文档 §6.3）：

```
E1 匿名：新建 → 生成需求类型1 → 追加需求类型2 → 刷新页面 → 数据仍在 → 回滚到 v1 → 再追加一轮
E2 登录：登录后重复 E1；无痕窗口确认看不到 E1/E2 的项目
E3 重启后端：pending/running 被置 failed 且文案可读
E4 密钥失效：临时改坏 APP_AI_KEY → 鉴权失败文案 + 描述未丢 + 库中 error 非空 + error_type=auth
E5 沙箱：生成页内执行 window.parent.document 必须抛跨域错误（SC-005）
```

---

## 7. 文件清单与施工顺序

### 7.1 顺序（依赖关系决定，不并行打乱）

| 阶段 | 内容 | 依赖 |
|---|---|---|
| S1 | 归属隔离：`dependencies/owner.py` + 9 路由过滤 + 数据迁移 + 删 `owner_key` 入参 | — |
| S2 | 前端接线：认证三态 + 登录入口 + 登出清态；删 `getOwnerKey`/`lib/auth.ts` | S1 |
| S3 | 生成稳定性：错误分类/退避/超时/心跳/上下文预算/结构校验 | — |
| S4 | 测试地基：`conftest.py` + `FakeAIHub` + 可注入 pipeline + inline 开关 | — |
| S5 | 回滚 + 一致性：restore 接口 + `VersionSwitcher` + 过渡态修复 + 哈希标识 | S1, S4 |
| S6 | 全量回归：§6 全绿 + 验收证据归档到 `docs/验收证据/` | S1–S5 |

S1 与 S3 相互独立，可先做 S1+S2 独立上线（风险最高、收益最大，且与生成链路解耦）。

### 7.2 新增

```
app/backend/dependencies/owner.py
app/backend/scripts/backfill_demo_owner.py
app/backend/tests/conftest.py
app/backend/tests/fakes.py
app/backend/tests/test_owner_isolation.py
app/backend/tests/test_pipeline_recovery.py
app/backend/tests/test_two_round_increment.py
app/backend/tests/test_version_restore.py
app/backend/tests/test_stale_recovery.py
docs/验收证据/A1-A7-*.md
```

### 7.3 修改

```
app/backend/routers/atoms.py         9 路由归属；删 owner_key 入参；_recover_stale_versions 按 owner + updated_at；
                                     restore 路由；generate 历史查询加 status='succeeded'；演示只读 409；迁移自检
app/backend/services/pipeline.py     __init__ 可注入 ai；错误分类/退避/wait_for 超时/心跳；
                                     looks_well_formed 判定；上下文预算；续写 history 截断；IntegrityError → 409
app/backend/services/html_extract.py looks_well_formed；确认 inject_csp 幂等
app/backend/services/prompts.py      HISTORY_ITEM_CHARS 400 + 最近一条 600；PREVIOUS_HTML_MAX_CHARS /
                                     CONTINUE_HISTORY_MAX_CHARS 与截断函数
app/backend/services/aihub_errors.py UpstreamError
app/backend/data_models/projects.json  仅更新 owner_key 的 description
app/backend/tests/test_html_extract.py      增补 CSP 幂等、结构校验
app/backend/tests/test_context_memory.py    随 HISTORY_ITEM_CHARS 调整
app/frontend/src/App.tsx             挂 AuthProvider
app/frontend/src/contexts/AuthContext.tsx   改写为 client.auth.* 三态
app/frontend/src/pages/Index.tsx     登录/登出入口 + 登出清态；版本切换清空 HTML 与加载态；回滚入口；预览区失败原因条
app/frontend/src/lib/atoms.ts        anon 头与 anon_key 持久化；restoreVersion；error_type/html_sha256 类型
app/frontend/src/components/VersionSwitcher.tsx  回滚入口 + 二次确认
app/frontend/src/components/PreviewPane.tsx      版本号/长度/哈希标识；失败原因条
app/frontend/src/components/CodeViewer.tsx       同上标识（与预览一致）
app/frontend/src/lib/constants.ts    删除 getOwnerKey
```

### 7.4 删除

```
app/frontend/src/lib/auth.ts          依赖后端并不返回的 redirect_url；唯一消费者已被改写
app/frontend/src/lib/constants.ts::getOwnerKey  客户端可伪造，且全仓无调用点
```

### 7.5 禁止改动（平台保护）

`app/backend/core/**`、`app/backend/models/**`、`app/backend/main.py`、`app/backend/lambda_handler.py`、`app/frontend/src/pages/AuthCallback.tsx`、`PreviewPane.tsx` 的 `SANDBOX_ATTR` 常量（`allow-scripts allow-forms`，**永不**加 `allow-same-origin`）。

归属过滤只放在 `routers/atoms.py`，不碰 `models`。

---

## 8. 与来源文档的偏离

来源文档 `docs/优化文档-无登录数据隔离与生成链路修复.md` 与本设计冲突时，以本设计为准。六处偏离均已与业主确认。

| # | 来源文档 | 本设计 | 理由 |
|---|---|---|---|
| 1 | 只在 `list_projects`/`create_project` 下发 cookie（§2.2①） | 所有路由共用依赖，无标识即签发（幂等） | `generate` 也可能成为入口（旧标签页直接提交）；统一后无分支，不依赖「前端一定先调列表」这一隐含假设 |
| 2 | 心跳直接写 `versions.updated_at`（§3.2②） | 心跳用独立的 `db_manager.session()` 短事务 | SQLAlchemy `AsyncSession` 禁止被两个协程并发使用；AI 调用在飞时共用同一 session 会出错 |
| 3 | 改 `services/aihub.py` 的 `AsyncOpenAI(timeout=…)`/`max_retries`（§3.2②） | 不改 `aihub.py`；pipeline 侧 `asyncio.wait_for` 超时 | `aihub.py` 是平台通用层，`routers/aihub.py` 亦共用；改它会把行为变更外溢到本项目之外。`wait_for` 效果等价且零外溢 |
| 4 | 演示区判据用 `owner_key IS NULL`（§2.2③ 方案 A） | 判据用 `is_demo`，迁移即置 `is_demo=true` | mock 数据本就是 `is_demo=true`；单一判据。迁移漏跑时后果是那些项目对所有人不可见（fail-closed 安全侧），而非人人可见 |
| 5 | 前端自算哈希（WebCrypto / length+djb2）（§5.2 问题 3） | 后端 `GET /versions/{seq}` 返回 `html_sha256`，两个工具条渲染同一个值 | 否则「一致」只是两处独立计算碰巧相等；同一个值既构造性一致，又真的校验了传输无损 |
| 6 | 修好 `AuthContext` 或改用 `client.auth`「二选一」（§2.2④） | 改写 `AuthContext.tsx` 走 `client.auth.*`，删除 `lib/auth.ts` | `lib/auth.ts` 只被 `AuthContext` 引用；`ProtectedAdminRoute` 全局无引用，改写后其行为反而正确 |

---

## 9. 不做的事

- 不引入真实账号体系、不新增用户表（平台内置用户表由平台管理）。
- 不做匿名 ↔ 登录身份的数据合并（Q2：列为后续项）。
- 不做「指针式回滚」，不新增 `projects.current_version_seq` 列（Q4）。
- 不改 `_RUNNING_TASKS` 的进程内实现（多 worker 取消是已知限制，已记录于 CLAUDE.md）。
- 不改沙箱策略、不改 `CODE_SYSTEM`/`DESIGN_SYSTEM` 的沙箱约束段落。
- 不做与本次目标无关的重构。
