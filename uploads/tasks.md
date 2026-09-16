# Tasks: 智能体驱动的应用生成平台（Atoms Demo）

**Feature**: `001-atoms-demo` | **Date**: 2026-09-15

**Input**: Design documents from `/specs/001-atoms-demo/`

**Prerequisites**: [plan.md](./plan.md)、[spec.md](./spec.md)、[research.md](./research.md)、[data-model.md](./data-model.md)、[contracts/](./contracts/)

**Tests**: 聚焦高风险纯逻辑（plan.md「不做全面覆盖」）。仅三处强制：`html_extract` 三级容错、SSE 跨 chunk 解析、错误信封在流开始前返回。

**Organization**: 按用户故事分阶段，每个故事可独立实现与验证。

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可并行（不同文件、无未完成依赖）
- **[Story]**: 所属用户故事（US1–US4）
- 所有路径均相对仓库根目录

---

## 阶段依赖总览

```
Phase 1 Setup ──► Phase 2 Foundational ──┬──► Phase 3 US1 (P1) 🎯 MVP
                                          ├──► Phase 4 US2 (P2)
                                          ├──► Phase 5 US3 (P3)
                                          └──► Phase 6 US4 (P4)
                                                     │
                        Phase 7 部署 ◄───────────────┘（需 US1–US3 完成）
                              │
                        Phase 8 Polish
```

**⚠️ 一处真实耦合（非人为拆分）**：US1 与 US4 共享 SSE 通道。US1 铺设**传输**（事件能流到前端）并交付**静态步骤骨架**以满足 AC3「3 秒内首个可见反馈」；US4 在其上构建**体验与持久化**（真实事件驱动、`step_delta` 思考片段、步骤回放）。**US4 必须在 US1 之后做**——这是顺序依赖，不是并行冲突。

---

## Phase 1: Setup（共享基础设施）

**Purpose**: 仓库初始化与依赖就位

- [ ] T001 在仓库根执行 `git init`，确认 `git status` 中**不出现** `backend/app/config.py` 与 `deploy/.env`；确认 `backend/app/config.example.py` 被纳入跟踪
- [ ] T002 [P] 创建 `backend/requirements.txt`：`fastapi`、`uvicorn[standard]`、`sqlalchemy>=2`、`pymysql`、`cryptography`（PyMySQL 连 MySQL 8 的 `caching_sha2_password` 必需）、`pydantic>=2`、`pydantic-settings`、`httpx`、`openai`、`pytest`、`pytest-asyncio`
- [ ] T003 [P] 初始化前端脚手架 `frontend/`：Vite + Vue 3 + TypeScript + Pinia + Monaco Editor（`monaco-editor`）；配置 `vite.config.ts` 将 `/api` 代理到 `http://127.0.0.1:8000`（**dev 代理是跨域问题的根治手段，不依赖后端 CORS**）
- [ ] T004 [P] 创建空目录占位：`backend/tests/`、`deploy/`

**Checkpoint**: 依赖可安装，前后端骨架可启动

---

## Phase 2: Foundational（阻塞性前置——所有用户故事的前置条件）

**Purpose**: 数据层、错误约定、模型客户端。**未完成前任何用户故事都不得开工**

**⚠️ CRITICAL**: 本阶段完成前 US1–US4 均无法开始

- [ ] T005 [P] 实现 `backend/app/db.py`：SQLAlchemy 2.x `create_engine` + `sessionmaker`；从 `app.config` 读取连接参数；连接串显式指定 `charset=utf8mb4`（**遗漏此项会让 emoji 与四字节字符写入失败**）；提供 FastAPI 依赖注入用的 `get_db()`
- [ ] T006 [P] 实现 `backend/app/models.py`——四张表，字段约束**逐字**对齐 [data-model.md](./data-model.md)：
  - `projects`：`public_id CHAR(36) UNIQUE NOT NULL`（对外标识，UUID v4）、`title VARCHAR(120) NOT NULL`、`created_at`/`updated_at DATETIME(3) NOT NULL`；索引 `UNIQUE(public_id)`、`INDEX(updated_at)`
  - `versions`：`project_id` FK→`projects.id` **ON DELETE CASCADE**、`seq INT UNSIGNED NOT NULL`、`prompt TEXT NOT NULL`、`html MEDIUMTEXT NULL`、`summary JSON NULL`、`status VARCHAR(16) NOT NULL`（枚举 `pending`/`running`/`succeeded`/`failed`）、`error TEXT NULL`、`duration_ms INT UNSIGNED NULL`；索引 `UNIQUE(project_id, seq)`、`INDEX(project_id, created_at)`
  - `messages`：`project_id` FK **ON DELETE CASCADE**、`role VARCHAR(16) NOT NULL`（`user`/`assistant`）、`content TEXT NOT NULL`、`version_id` FK→`versions.id` **ON DELETE SET NULL** 可空
  - `generation_steps`：`version_id` FK **ON DELETE CASCADE**、`seq TINYINT UNSIGNED NOT NULL`（1..3）、`name VARCHAR(60) NOT NULL`、`status VARCHAR(16) NOT NULL`、`output MEDIUMTEXT NULL`、`started_at`/`ended_at DATETIME(3) NULL`；索引 `UNIQUE(version_id, seq)`
  - 建表时**统一指定** `mysql_charset='utf8mb4'`、`mysql_collate='utf8mb4_0900_ai_ci'`、`mysql_engine='InnoDB'`
- [ ] T007 [P] 实现 `backend/app/schemas.py`：Pydantic v2 请求/响应模型。**关键约束**：`GenerateRequest.prompt` 去首尾空白后长度 **1–2000**；所有响应模型**不得包含自增 `id`**（[rest-api.md](./contracts/rest-api.md) 硬性要求：防止项目被枚举遍历）
- [ ] T008 实现 `backend/app/errors.py`：统一错误信封 `{"error":{"code":...,"message":...}}`，覆盖 `VALIDATION_ERROR(400)`、`NOT_FOUND(404)`、`CONFLICT(409)`、`UPSTREAM_ERROR(502)`、`INTERNAL_ERROR(500)`；注册全局异常处理器。**`message` 必须是面向用户的可读中文，不得含堆栈或内部路径**（会被前端直接展示）
- [ ] T009 实现 `backend/app/services/llm.py`：DeepSeek 客户端（OpenAI 兼容，`base_url` 从 config 读取）；流式接口产出增量文本；`MODEL` 固定走配置项**不硬编码**。
  - **⚠️ 启动探测必须校验 `content` 非空**，而非仅看 HTTP 状态码——实测 `deepseek-flash` 会返回 HTTP 200 但内容为空串（[research.md](./research.md) R4）
  - 探测结果缓存 60 秒，避免健康检查本身消耗配额
  - `API_KEY` 为空时不得抛异常，须降级为 `configured=false` 便于本地无 key 调试
- [ ] T010 实现 `backend/app/main.py`：FastAPI 应用装配、lifespan（启动时建表）、路由挂载、静态产物托管（生产由 Nginx 承担，此处仅兜底）；**生产环境不得开启 `allow_origins=["*"]`**，CORS 仅用于本地联调
- [ ] T011 [P] 实现 `backend/app/routers/health.py`：`GET /api/health` 返回 `{"status","db","llm":{"configured","model","reachable"}}`（[rest-api.md](./contracts/rest-api.md)）。DB 与 LLM 探测须各自超时兜底，任一失败不得让接口 500
- [ ] T012 [P] 实现 `frontend/src/api/client.ts`：REST 封装，**统一解析错误信封**并把 `error.message` 直接作为可展示文案抛出

**Checkpoint**: 数据库可连、健康检查通过、错误约定就位——用户故事可开工

---

## Phase 3: User Story 1 — 一句话生成可运行的应用 (P1) 🎯 MVP

**Goal**: 输入一段描述 → 生成 → 渲染为**可真实交互**的应用

**Independent Test**: 空白页输入「做一个记账小工具，能记收入和支出，显示总余额」并提交，能观察到生成结果被渲染出来，且可在其中**输入金额并看到余额真实变化**。不依赖历史记录、迭代、过程可视化。

### Tests for User Story 1（先写，确认失败后再实现）

- [ ] T013 [P] [US1] `backend/tests/test_html_extract.py`——覆盖 [research.md](./research.md) R6 要求的三条路径与畸形输入：①Markdown 围栏 ` ```html ` ②首个 `<!DOCTYPE html>` 或 `<html` 至文末 ③整体兜底；畸形输入：空串、仅围栏无内容、未闭合标签、含前置说明文字
- [ ] T014 [P] [US1] `frontend/src/api/__tests__/stream.spec.ts`——SSE 解析纯函数测试。**必须覆盖跨 chunk 截断**（一个完整帧被 TCP 分片切成多次 `read()` 返回）与 `:` 开头心跳行的忽略。契约明确这是「最容易写错、也最难排查」的点，表现为偶发丢事件
- [ ] T015 [P] [US1] `backend/tests/test_api.py`——错误信封测试。**核心断言：`VALIDATION_ERROR`(400)、`NOT_FOUND`(404)、`CONFLICT`(409) 必须全部在 SSE 流开始之前以普通 JSON 返回**（响应头须为 `application/json` 而非 `text/event-stream`）——一旦响应头以 `text/event-stream` 发出就无法再改状态码，这是契约中明确标注的「关键」项。另须断言 `error.message` 为可读中文、不含堆栈与内部路径

### Implementation for User Story 1

- [ ] T016 [US1] 实现 `backend/app/services/html_extract.py`（**纯函数、无副作用**）：三级容错提取（对齐 T013）+ **CSP 注入**。注入内容为 `default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data: blob:`（[streaming-events.md](./contracts/streaming-events.md) 渲染约束）
- [ ] T017 [P] [US1] 实现 `backend/app/services/prompts.py`：三阶段提示词。阶段 1 需求分析（输出应用名与功能要点）、阶段 2 结构设计、阶段 3 代码生成（**强约束：只返回 HTML，不要 Markdown 围栏与解释文字**）。提示词须明确「单个自包含页面」（FR-015）
- [ ] T018 [US1] 实现 `backend/app/services/pipeline.py`：三阶段串行编排（[research.md](./research.md) R1）。**先落库后执行**（R10）——提交时即创建 `versions` 记录并**预置 3 条 `generation_steps`（`status='pending'`）**，使前端提交后立即拿到完整步骤列表。产出 SSE 事件序列 `run_started → (step_started → step_delta* → step_completed)* ×3 → run_completed`；任一阶段失败发 `step_failed` 后**必须紧跟 `run_failed`**。成功后校验结果以 `</html>` 结尾，不满足则置 `failed` 而非静默展示残缺页面（[research.md](./research.md) 风险 3）
- [ ] T019 [US1] 实现 `backend/app/routers/generate.py`：`POST /api/projects/{public_id}/generate`，SSE 响应。**响应头必须含 `X-Accel-Buffering: no`**（缺失会导致 Nginx 把流攒到最后一次性吐出，流式完全失效）。每 15 秒发 `: keep-alive` 注释行。**⚠️ `VALIDATION_ERROR` 与 `CONFLICT` 必须在响应头以 `text/event-stream` 发出之前以普通 JSON 返回**——一旦发出响应头就无法再改状态码
- [ ] T020 [US1] 实现 `frontend/src/api/stream.ts`：`fetch` + `ReadableStream` 手动解析 SSE。**不可使用 `EventSource`**——它只支持 GET 且无法携带请求体（[streaming-events.md](./contracts/streaming-events.md)）。须维护跨 chunk 缓冲区（通过 T014）
- [ ] T021 [US1] 实现 `frontend/src/stores/workspace.ts`（Pinia）：当前项目、生成状态、步骤数组、当前 HTML、提交动作。**提交瞬间即在本地构造 3 条步骤骨架并渲染，不等待任何网络往返**——这是 SC-002「3 秒内首个可见反馈」的实现方式
- [ ] T022 [P] [US1] 实现 `frontend/src/components/PromptInput.vue`：描述输入与提交，生成中禁用重复提交（配合 T019 的 409）
- [ ] T023 [P] [US1] 实现 `frontend/src/components/PreviewPane.vue`：**安全关键组件**。`<iframe sandbox="allow-scripts allow-forms" srcdoc="...">`——**sandbox 属性必须以常量硬编码，不接受 props 或配置注入**；**严禁添加 `allow-same-origin`**（与 `allow-scripts` 同时存在时 iframe 将获得与父页面相同的源，可读取父页面 DOM 与 cookie，等于完全绕过隔离）
- [ ] T024 [US1] 实现 `frontend/src/components/AgentSteps.vue`：**展示型组件**，由 props 接收步骤数组并渲染。US1 阶段仅需静态骨架与基本状态，真实事件驱动由 US4 完成
- [ ] T025 [US1] 实现 `frontend/src/views/Workspace.vue`：主工作区布局，装配 T022–T024，接 T021 store
- [ ] T026 [US1] 端到端自测（[quickstart.md](./quickstart.md) V1）：确认生成结果可交互且**余额数字真实变化**（SC-004，非静态占位）；确认提交后 3 秒内有可见反馈

**Checkpoint**: US1 完整可用且可独立验证——**此时已构成可演示的 MVP**

---

## Phase 4: User Story 2 — 生成结果被保存，随时可以回来 (P2)

**Goal**: 刷新页面或重启服务后，项目、生成结果、对话记录完整找回

**Independent Test**: 生成一个应用 → **重启后端服务** → 重新打开平台 → 确认项目、生成结果与对话记录均可完整找回。

**依赖**: 需 US1 完成（要有可保存的生成结果）

### Implementation for User Story 2

- [ ] T027 [US2] 实现 `backend/app/routers/projects.py` 的读取接口：`GET /api/projects`（按 `updated_at` 倒序，返回 `version_count` 与 `latest_status`，**不含 `html`**）、`GET /api/projects/{public_id}`（含 `versions` 列表与 `messages`，**不含 `html`**）
- [ ] T028 [US2] 在 `backend/app/routers/projects.py` 实现 `POST /api/projects`（创建空项目，`title` 可选，缺省用「未命名项目」占位，首次生成完成后由阶段 1 产出覆盖）与 `DELETE /api/projects/{public_id}`（204，级联删除）
- [ ] T029 [US2] 在 `backend/app/routers/projects.py` 实现 `GET /api/projects/{public_id}/versions/{seq}`：返回**含 `html`** 的完整版本内容。HTML 体积大，仅此接口返回
- [ ] T030 [US2] 实现启动清理逻辑（`backend/app/main.py` lifespan 或 `services/recovery.py`）：服务重启后把残留的 `pending`/`running` 版本置为 `failed` 并写入**面向用户的可读原因**，避免用户看到永久卡住的加载态（[data-model.md](./data-model.md) 恢复语义）
- [ ] T031 [P] [US2] 实现 `frontend/src/components/ProjectList.vue`：历史项目列表，展示标题、更新时间、版本数与最新状态
- [ ] T032 [US2] 在 `frontend/src/views/Workspace.vue` 与 `frontend/src/stores/workspace.ts` 接入项目列表：首页加载时经 `frontend/src/api/client.ts` 拉取 `GET /api/projects`；点击项目 → 拉详情 + 最新版本 HTML → 渲染进 `frontend/src/components/PreviewPane.vue`
- [ ] T033 [US2] 实现 `frontend/src/components/CodeViewer.vue`：Monaco 只读代码视图（FR-009）
- [ ] T034 [US2] 端到端验证（[quickstart.md](./quickstart.md) V2）——**必验项**：刷新页面数据在；**重启后端服务**后数据仍在（FR-004 核心，也是原始需求「数据持久化」的验收点）
- [ ] T035 [US2] 验证失败路径（V5）：将 key 改为无效值提交一次，确认 ①界面给出可读中文提示不含堆栈 ②**用户已输入的描述未丢失**（FR-011）③库中该版本 `status='failed'` 且 `error` 非空

**Checkpoint**: US1 + US2 均独立可用——刷新与重启不再丢数据

---

## Phase 5: User Story 3 — 在已有结果上继续提要求 (P3)

**Goal**: 在既有项目上追加要求 → 产出新版本，旧版本保留可切换

**Independent Test**: 对已有项目追加一次修改要求，确认产生新版本（`seq` 递增）且旧版本仍可访问、可切换回去。

**依赖**: 需 US1、US2 完成

### Implementation for User Story 3

- [ ] T036 [US3] 扩展 `backend/app/services/pipeline.py` 支持多轮上下文：只回传**上一版 HTML + 本次新指令**，**不回传完整对话历史**（[research.md](./research.md) R5——全量历史会导致上下文随轮次线性膨胀，成本与时延持续恶化）。提示词须明确「在原有基础上改进，保留已有功能」对应 FR-006
- [ ] T037 [US3] 扩展 `backend/app/routers/generate.py`：在已有项目上创建 `seq+1` 版本；每次生成同时写入 `messages`（`user` 消息关联为空，`assistant` 消息关联对应 `version_id`）——助手消息**只存展示用摘要，不重复存 HTML**
- [ ] T038 [US3] 在 `backend/app/routers/projects.py` 实现同一项目下的版本查询支持：`GET /api/projects/{public_id}` 的 `versions` 列表按 `seq` 返回历次记录，供版本切换使用
- [ ] T039 [P] [US3] 实现 `frontend/src/components/VersionSwitcher.vue`：版本列表与切换（FR-007），切换时按需拉取该版本 HTML
- [ ] T040 [P] [US3] 实现 `frontend/src/components/ConversationPanel.vue`：展示 `messages` 对话记录（用户要求 + 系统响应）
- [ ] T041 [US3] 在 `frontend/src/views/Workspace.vue` 装配 T039、T040，并在已有项目下把输入框语义从「新建」切换为「追加修改」
- [ ] T042 [US3] 端到端验证（[quickstart.md](./quickstart.md) V3）：追加「再加一个按月份筛选的图表」，确认 ①`seq` 递增 ②新版本**保留原有记账功能**而非推倒重来 ③可切回 `seq=1` 并正确渲染
- [ ] T043 [US3] 验证并发约束（V6）：生成进行中对同项目再次提交，确认返回 `409 CONFLICT` 且提示文案用户可读（FR-012）

**Checkpoint**: US1–US3 全部独立可用——**P1–P3 必达范围完成**

---

## Phase 6: User Story 4 — 看见智能体在工作 (P4)

**Goal**: 分步骤工作流实时推进，能看到智能体当前的思考片段

**Independent Test**: 提交一次描述，确认界面出现分步骤状态展示，且各步骤状态**随真实进程推进**（非定时器伪造）。

**依赖**: **必须在 US1 之后**（复用其 SSE 通道与 `AgentSteps.vue`）

### Implementation for User Story 4

- [ ] T044 [US4] 扩展 `backend/app/services/pipeline.py`：每阶段开始时把 `generation_steps` 置 `running` 并写 `started_at`；结束时置 `succeeded` 并写 `ended_at` 与该阶段原始产出 `output`（为回放提供基础）
- [ ] T045 [US4] 在 `backend/app/routers/generate.py` 补齐事件发射：`step_started` / `step_delta` / `step_completed` 三类事件的完整载荷（[streaming-events.md](./contracts/streaming-events.md)）。**`step_delta` 为可选事件——前端须能在完全不收到它的情况下正常工作**，不可让它成为必要路径
- [ ] T046 [US4] 扩展 `frontend/src/stores/workspace.ts`：把 T020 解析出的事件**真实映射**到步骤状态，替换 US1 阶段的静态骨架
- [ ] T047 [US4] 扩展 `frontend/src/components/AgentSteps.vue`：接真实事件驱动状态流转（等待 → 进行中 → 已完成/失败）；订阅 `step_delta` 展示智能体思考片段（打字机效果或增量追加）
- [ ] T048 [US4] 步骤回放：打开历史项目时，从 `GET /api/projects/{public_id}` 的 `versions[].steps` 恢复各步骤状态并与对话记录一同展示
- [ ] T049 [US4] 失败粒度展示：`step_failed` 时在**对应步骤**上显示失败态与可读原因，而非只在全局提示（这是步骤记录为排查提供阶段粒度定位能力的体现）
- [ ] T050 [US4] 端到端验证（[quickstart.md](./quickstart.md) V1 与 P4 独立测试）：确认步骤状态随真实进程推进，且**在完全收不到 `step_delta` 的降级情况下界面仍正常**

**Checkpoint**: 四个用户故事全部独立可用

---

## Phase 7: 部署与公开访问（交付硬要求）

**Purpose**: 原始需求要求提供「可测试的在线访问链接」，评估维度含「可交付性」

**依赖**: 需 US1–US3 完成。**US4 可与本阶段并行**

- [ ] T051 配置 `deploy/docker-compose.yml`：`nginx` + `api` + `mysql` 三服务编排；**MySQL 端口不对宿主暴露**，仅容器内网络可达；密钥通过环境变量注入，`docker-compose.yml` 中**不得出现明文**
- [ ] T052 配置 `deploy/Dockerfile.api` 与 `deploy/nginx.conf`：Nginx 直接托管 Vue 构建产物、`/api` 反代至 `api` 服务。**SSE 反代必须关闭缓冲**——`proxy_buffering off`、`proxy_cache off`、`proxy_read_timeout` 大于单次生成上限（否则流被攒到最后一次性吐出）
- [ ] T053 创建 `deploy/.env.example`（对齐 `.gitignore` 已预留的排除规则）
- [ ] T054 创建 `deploy/mysql/init.sql`：建库 `atoms_demo` 并显式指定 `CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci`（**依赖 MySQL 镜像默认字符集是不可靠的**，务必显式指定）；由 `docker-compose.yml` 挂载到 `/docker-entrypoint-initdb.d/`
- [ ] T055 **⚠️ 在服务器所在网络重新实测 DeepSeek 连通性与延迟**——本地实测在本地网络完成，**两地网络质量未必一致**（[research.md](./research.md) 风险 4，仍在待验证状态）：
      `curl -o /dev/null -s -w "connect=%{time_connect}s total=%{time_total}s\n" https://api.deepseek.com/chat/completions`
      若总延迟导致 SC-001 的 90 秒指标有风险，把代码生成阶段切到 `deepseek-v4-pro` 并复测
- [ ] T056 配置 TLS（方案见 [research.md](./research.md) R8，**待用户确认**：购买低价域名 + Let's Encrypt，或使用 `IP:端口` 无 HTTPS 兜底）
- [ ] T057 部署后核验（[quickstart.md](./quickstart.md) 部署验证）：①首页可打开 ②`/api/health` 返回三字段正常 ③**观察 Network 面板确认事件渐进到达而非最后一次性返回**（验证 SSE 未被缓冲）④从外部连接 3306 应被拒绝 ⑤从**非服务器所在网络**的机器匿名打开链接

**Checkpoint**: 公开可访问的演示链接就绪

---

## Phase 8: Polish 与交付收尾

- [ ] T058 [P] 撰写 `README.md`：实现思路、关键取舍（沙箱隔离、SSE 而非轮询、三阶段流水线的理由）、完成程度、**后续扩展优先级**（对应 spec 的分期交付策略）
- [ ] T059 [P] 撰写 `docs/roadmap.md`：「延展能力」与后续扩展优先级。**延展能力后备池**为 FR-015「多文件工程升级」与 FR-016「应用级持久化」，二者共享同一份剩余时间预算、**择一推进**。须说明实际选了哪一个、为什么，以及未选的那个如何继续推进（**避免两个半成品**）
- [ ] T060 编写 `backend/scripts/seed_demo.py` 并执行：写入演示种子数据，确保评估者首次打开时**项目列表不为空**（SC-006 与「评估场景」假设）
- [ ] T061 **⚠️ 密钥泄露核验**：对全仓执行 `grep -rE "sk-[A-Za-z0-9]{20,}" .` 确认**无命中**；确认 `git status` 中不含 `backend/app/config.py` 与 `deploy/.env`（plan.md 门禁 G2）
- [ ] T062 确认仓库权限为 **public**，且 Demo 链接可用**无痕窗口匿名打开**（FR-014：无需注册登录）
- [ ] T063 [P] 确认 `backend/app/config.example.py` 已提交且 Key 字段为空
- [ ] T064 截图 AI 工具使用账单并存入 `docs/billing/`（原始需求中的加分项）
- [ ] T065 复跑 [quickstart.md](./quickstart.md) V1–V6 全部场景，确认无回归
- [ ] T066 确认沙箱隔离可被验证（V4）：iframe 的 `sandbox` 不含 `allow-same-origin`；在生成的应用内执行 `window.parent.document` **抛出跨域错误**（SC-005 可验证性）

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 Setup**：无依赖，可立即开始
- **Phase 2 Foundational**：依赖 Phase 1 —— **阻塞全部用户故事**
- **Phase 3–6 用户故事**：均依赖 Phase 2
- **Phase 7 部署**：依赖 US1–US3（US4 可并行）
- **Phase 8 Polish**：依赖全部所需故事完成

### User Story Dependencies

| 故事 | 依赖 | 说明 |
|---|---|---|
| **US1 (P1)** | Phase 2 | 无故事间依赖，可独立完成 |
| **US2 (P2)** | **US1** | 需先有可保存的生成结果 |
| **US3 (P3)** | **US1, US2** | 迭代建立在持久化之上 |
| **US4 (P4)** | **US1** | **共享 SSE 通道**，须在 US1 之后顺序进行 |

### Within Each User Story

- 测试先写并确认**失败**，再实现
- 模型 → 服务 → 接口 → 前端集成
- 一个故事完成并验证后再进入下一个优先级

---

## Parallel Opportunities

**Phase 1**：T002、T003、T004 可并行
**Phase 2**：T005、T006、T007 可并行（不同文件）；T008 独立；T012 与后端解耦可并行
**US1**：T013、T014、T015 三个测试任务可并行（不同文件）；T017 与 T016 可并行；T022、T023 可并行（不同组件）
**US2**：T031 与后端接口任务可并行
**US3**：T039、T040 可并行（不同组件）
**Phase 8**：T058、T059、T063 可并行

```bash
# US1 测试并行启动：
Task: "test_html_extract.py 覆盖三级容错与畸形输入"
Task: "stream.spec.ts 覆盖跨 chunk 截断与心跳行忽略"

# US1 组件并行启动：
Task: "PromptInput.vue 输入与提交"
Task: "PreviewPane.vue 沙箱 iframe"
```

---

## Implementation Strategy

### MVP First（仅 US1）

1. Phase 1 Setup
2. Phase 2 Foundational（**关键路径，阻塞一切**）
3. Phase 3 US1
4. **STOP 并独立验证**：按 quickstart V1 确认生成结果可交互
5. 可部署演示

**注意**：仅 US1 不满足原始需求的「数据持久化」硬性要求，**不可作为最终交付**——它是里程碑而非终点。

### Incremental Delivery

1. Setup + Foundational → 地基就绪
2. **+ US1 → 验证 V1 → 可演示（MVP）**
3. **+ US2 → 验证 V2 → 数据不丢，脱离「玩具」判定**（原始需求的核心验收点）
4. + US3 → 验证 V3 → 具备迭代能力
5. + US4 → 体验差异化，对应评估维度中的「创新性」
6. + 部署 → 交付公开链接

### 风险提示

- **T009 的探测逻辑**与 **T019 的响应头/流前校验**是本项目两个「错了就整体失效」的点：前者错则生成内容为空，后者错则流式完全失效。实现后应立即验证，不要等到集成阶段
- **T055 不可跳过**——本地实测结论不能外推到服务器网络，这是当前风险登记表中唯一「已验证但仍存疑」的项
