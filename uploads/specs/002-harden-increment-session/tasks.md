---
description: "Task list for 增量可靠性与会话连续性加固"
---

# Tasks: 增量可靠性与会话连续性加固

**Feature**: `002-harden-increment-session`
**Input**: 设计文档来自 `uploads/specs/002-harden-increment-session/`
**Prerequisites**: [plan.md](./plan.md)、[spec.md](./spec.md)、[research.md](./research.md)、[data-model.md](./data-model.md)、[contracts/](./contracts/)

**Tests**: **本特性明确要求测试证据**——spec 的 FR-030~FR-035 与 SC-012 直接规定了证据形态（产物级核对、非模拟路径的取消证据、独立于产出方的三方对照、全绿基线）。因此每个用户故事都包含测试任务，且测试先于实现编写并确认初始为**红**。

**组织方式**: 按用户故事分组，每个故事可独立实现与独立验证。

**运行约定（必须遵守）**:
- 后端测试一律 `cd app/backend && python -m pytest ...`（仓库根无 conftest/pyproject，靠 `python -m` 把 CWD 放进 `sys.path`；直接 `pytest` 会 ImportError）
- 前端检查 `cd app/frontend && npm run lint && npm run build`
- 平台约束：不可改 `app/backend/core/**`、`models/**`、`main.py`、`lambda_handler.py`；不新建用户表；`routers/` 自动发现且前缀必须 `/api/v1/`；错误信封恒为 `{"error":{"code","message"}}`

## Format: `[ID] [P?] [Story] Description`

- **[P]**: 可并行（不同文件、无未完成依赖）
- **[Story]**: 所属用户故事（US1 / US2 / US3）
- 每个任务都给出精确文件路径

## 路径约定

- 后端：`app/backend/`
- 前端：`app/frontend/src/`
- 规格与证据：`uploads/specs/002-harden-increment-session/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: 建立可复跑的留档与红灯基线。本特性的一切验收都依赖「有证据」，所以留档机制必须先落地。

- [ ] T001 [P] 创建证据留档目录 `uploads/specs/002-harden-increment-session/evidence/` 并写 `README.md`，说明命名约定（`<日期>-<被测版本>-<场景>.txt`）与三个必留场景（本地回归、线上探针、两轮端到端）

- [ ] T002 [P] 录制红灯基线：在 `app/backend` 执行 `python -m pytest tests/ -q`，把完整输出（含 8 failed / 90 passed / 2 errors 的用例名）保存到 `uploads/specs/002-harden-increment-session/evidence/baseline-tests.txt`，作为后续「从红转绿」的对照物

**Checkpoint**: 留档机制可用、基线可对照 —— 后续每个任务都能产出可验证证据

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: 时间预算原语与自终结保证。**US1 的"有界等待"、US3 的全部失败语义都建立在这两个原语之上**，因此必须先于所有用户故事完成。

**⚠️ CRITICAL**: 本阶段完成前，不要开始任何用户故事。理由：没有整体预算，US1 的验收场景 2（最坏情况下有明确上限）无法成立；没有删除心跳，US3 的"卡死回收"整条链是死的。

- [ ] T003 在 `app/backend/services/pipeline.py` 声明 `GENERATION_BUDGET_SECONDS = 420.0`，并把 `STAGE_TIMEOUT` 由 240.0 降为 **120.0**、`MAX_ATTEMPTS` 由 3 降为 **2**、`RETRY_BACKOFF_SECONDS` 由 `(0.5, 1.0, 2.0)` 改为 `(0.5, 1.0)`；在常量块处附注释写明理由与不等关系 `120 < 420 < 600 < 720`（`STAGE_TIMEOUT=120` 取在上游实测 126.2s 返回 524 的切断点之内）

- [ ] T004 在 `app/backend/services/pipeline.py` 的流水线入口计算单调截止时刻 `deadline = monotonic() + GENERATION_BUDGET_SECONDS`，并新增 `_remaining()` 辅助；把 `_call_step` 内的调用超时由固定的 `STAGE_TIMEOUT` 改为 `min(STAGE_TIMEOUT, remaining)`（仅内存计算，**不得**引入跨 AI 调用的数据库事务）

- [ ] T005 在 `app/backend/services/pipeline.py` 实现**预算耗尽止损**：每个阶段发起调用**之前**若 `remaining` 低于最小可用调用预算，则不再发起调用，直接以 `PipelineError(..., error_type="budget_exhausted")` 落库失败，用户文案说明「本次生成超出时间预算，描述已保留，可重新提交」；该写入路径同样受 `if version.status in ACTIVE_STATUSES` 守卫（晚到的终态不得覆盖已写入的终态）

- [ ] T006 在 `app/backend/services/pipeline.py` **删除 `_heartbeat` 及其 `HEARTBEAT_INTERVAL` 常量**，并更新引用它的注释块；同时在原注释处写明确认结论：`versions.updated_at` 只在真实进展点（`_finish_step` 写摘要、`_fail` 写终态）推进，`_call_step` 置步骤 `running` 时改的是 `generation_steps` 行，**不**推进 `versions.updated_at`，故活任务最长静默期 = 阶段3 的预算 420s < `STALE_AFTER` 600s

- [ ] T007 [P] 新建 `app/backend/tests/test_time_budget_invariants.py`：断言 `GENERATION_BUDGET_SECONDS < STALE_AFTER.total_seconds() < FRONTEND_POLL_TIMEOUT`，其中 `FRONTEND_POLL_TIMEOUT` 从 `app/frontend/src/pages/Index.tsx` 的 `GENERATION_POLL_TIMEOUT_MS` **读取真实值**（不得在本测试内重新硬编码，否则又变成自证）

- [ ] T008 [P] 修复与身份无关的 6 个红灯，使这些用例转绿：`app/backend/tests/test_generation_inline.py`（3 个）、`app/backend/tests/test_fake_aihub.py`（1 个）、`app/backend/tests/test_conftest_smoke.py`（2 个 error）。根因是合并 `bd78a46` 拼接了两条谱系（实现文件来自一侧、测试由另一侧针对已被覆盖的实现编写），因此要么按当前实现修正断言，要么明确废弃并注明理由——**不得**为了让测试变绿而改动被测实现的行为

**Checkpoint**: 预算与自终结原语就位、时间不等关系有测试守护、非身份类红灯清空 —— 三个用户故事现在可以并行开始

---

## Phase 3: User Story 1 - 在已有项目上追加需求，旧功能必须还在 (Priority: P1) 🎯 MVP

**Goal**: 同一项目连续两轮增量，第二轮在保留第一轮全部既有能力的前提下产出新版本；两轮都在明确上限内到达终态。

**Independent Test**: 创建项目 → 生成第一轮 → 提交一句指代型追加需求 → 观察两轮是否都在上限内到达终态 → **逐项核对第一轮已实现的能力在第二轮产物中是否仍然存在**。不依赖登录、回滚、会话恢复。

### Tests for User Story 1 ⚠️

> **先写测试，确认初始为红，再实现**

- [ ] T009 [P] [US1] 在 `app/backend/tests/test_two_round_increment.py` 补测试：**增量基线必须是最近一个成功版本**——构造「第 1 轮成功、第 2 轮失败、第 3 轮请求」的序列，断言第 3 轮注入的 `previous_html` 是第 1 轮的产出，而不是第 2 轮的（也不为空）

- [ ] T010 [P] [US1] 在 `app/backend/tests/test_two_round_increment.py` 补测试：**指代解析生效**——断言第二轮请求的消息中同时包含历史上下文块与 `ANAPHORA_HINT` 的指示内容（当前 `ANAPHORA_HINT` 在 `tests/` 下零引用，属 spec 第 8 条点名的缺口）

- [ ] T011 [P] [US1] 在 `app/backend/tests/test_two_round_increment.py` 补测试：**上下文体积有上限**——覆盖 `PREVIOUS_HTML_MAX_CHARS` 与截断函数的实际生效（当前 `truncate_previous_html` / `truncate_continue_history` / `PREVIOUS_HTML_MAX_CHARS` / `CONTINUE_HISTORY_MAX_CHARS` 在 `tests/` 下零引用），断言超长输入被裁剪且轮次增加不导致体积线性增长

### Implementation for User Story 1

- [ ] T012 [US1] 在 `app/backend/services/pipeline.py` 核准并修正增量基线的选取：`generate` 传入的 `previous_html` 必须取该项目**最新 `succeeded` 版本**的页面；失败/取消版本不得进入候选（FR-002）

- [ ] T013 [US1] 在 `app/backend/services/pipeline.py` 核准并修正需求历史的构造：`history_prompts` 只含该项目此前**成功**版本的需求描述、按序升序、受 `HISTORY_MAX_ITEMS` × `HISTORY_ITEM_CHARS` 约束（FR-003/FR-005）

- [ ] T014 [US1] 扩展 `app/backend/e2e_two_round.py` 的**产物级**核对：对第二轮产物逐项断言第一轮已实现能力仍存在（计算器场景为 `+ - * / AC 显示 .`）、断言第二轮新增能力存在（`历史`）、断言长度比 `seq2/seq1 ≥ 0.80`、断言摘要自洽且以 `</html>` 结尾、断言第二轮结束后回取第一轮版本**逐字节不变**。仅断言"注入内容"不算通过（FR-006/FR-030）

**Checkpoint**: US1 可独立验证 —— 两轮增量在上限内到达终态，且旧功能保留有产物级证据

---

## Phase 4: User Story 2 - 刷新之后，回到我原来的地方 (Priority: P2)

**Goal**: 刷新或新标签页打开后身份不丢、项目与版本仍在且内容与校验指纹逐字节一致；进行中的生成自动接回；登出后浏览器侧不残留上一身份痕迹。

**Independent Test**: 创建项目并生成 → **完全丢弃内存状态**重新访问，仅凭浏览器侧持久化痕迹验证一致性与进行中接回 → 换全新身份验证看不到他人项目 → 登出后验证无残留。

### Tests for User Story 2 ⚠️

- [ ] T015 [P] [US2] 在 `app/backend/tests/test_owner_dependency.py` 修正/新增归属键测试并使 `test_distinct_long_subjects_yield_distinct_keys` 通过：三个不同 `sub`（长度 80 / 60 / 60、前缀相同）必须产出**三个不同** `owner_key`，且总长 ≤ 64

- [ ] T016 [P] [US2] 在 `app/backend/tests/test_owner_dependency.py` 使 `test_missing_secret_warns_once` 通过，并补一项加固测试：`_verify` 收到 `None` 或非十六进制形状的签名时**拒绝**而不是抛 `AttributeError` 变成 500

- [ ] T017 [P] [US2] 新建 `app/backend/tests/test_session_logout.py`：断言 `POST /api/v1/atoms/session/logout` 返回的 `anon_key` ≠ 旧值、`Set-Cookie` 同时下发新值、携新身份请求**看不到**旧身份的项目、连续调用幂等且各自返回可用新身份

- [ ] T018 [P] [US2] 在 `app/backend/tests/test_owner_dependency.py` 补 cookie 属性测试：HTTPS 请求的 `Set-Cookie` 含 `Secure`，HTTP 本地请求**不含**（保证本地开发与测试可回传）

### Implementation for User Story 2

- [ ] T019 [US2] 在 `app/backend/dependencies/owner.py` 把登录身份归属键改为 **`"user:" + sha256(subject).hexdigest()[:32]`**（总长 37，长度无关地唯一），恢复 `_SUBJECT_CHARS = 32` 常量与「密钥缺失时告警一次」的行为；同时给 `_verify` 加 `None` 守卫与签名十六进制形状校验（FR-021/FR-024，修掉已本地实证的 3→1 碰撞）

- [ ] T020 [US2] 在 `app/backend/routers/atoms.py` 的 `_json()` 给 `set_cookie` 补 `secure`：由**请求实际 scheme** 推导（`https` → `Secure`），并提供环境变量覆盖以应对网关未传递 `X-Forwarded-Proto` 的情况；本地 HTTP 必须保持可回传（contracts/rest-api.md 变更 3）

- [ ] T021 [US2] 在 `app/backend/routers/atoms.py` 新增 `POST /api/v1/atoms/session/logout`：删除旧 cookie 并**签发一个全新匿名身份**，经 `Set-Cookie` 与响应体两条通道一并下发，响应 `{"status":"ok","anon_key":"<新值>"}`；沿用既有错误信封与 `/api/v1/` 前缀（contracts/rest-api.md 变更 2）

- [ ] T022 [US2] 在 `app/frontend/src/lib/atoms.ts` 修正 `clearAnonKey` 的语义边界并新增登出调用：`HttpOnly` cookie 前端**原理上清不掉**，因此登出必须调用 T021 的后端端点（服务端下发删除 + 新身份），同时继续清 localStorage；确认身份优先级中 cookie 高于 `X-Atoms-Anon` 请求头这一事实已被正确处理

- [ ] T023 [US2] 在 `app/frontend/src/pages/Index.tsx` 实现**恢复上次打开的项目**：刷新后若存在上次打开的项目标识则自动载入（当前 `activeId` 初始为 `null`，工作区为空），并在切换/打开项目时清除旧 `html` 以避免闪现上一个项目的内容

- [ ] T024 [US2] 在 `app/frontend/src/pages/Index.tsx` 确认并补齐**进行中生成自动接回**：刷新时若存在 `pending`/`running` 版本则自动恢复 2.5s 轮询直至终态，用户无需重新提交（FR-027）

- [ ] T025 [US2] 新建 `app/backend/tests/test_source_preview_consistency.py` 实现**三方对照**（FR-033 / research.md R-10）：① `GET /versions/{seq}` 返回的 `html` 与 `html_sha256`；② 验证方**独立计算**的摘要（不得复用被测代码路径的公式）；③ 前端展示条与代码视图所依据的同一数据源。三者必须相等——用独立算法取代「同一公式复算同一响应体」的自证测试

**Checkpoint**: US2 可独立验证 —— 两条身份通道（cookie / header）都通过、跨身份隔离正确、登出无残留、刷新后逐字节一致

---

## Phase 5: User Story 3 - 失败必须说清楚，并且让我能重来 (Priority: P3)

**Goal**: 任何失败都在上限内落到明确终态，记录含失败阶段/类型/尝试次数，归因指向**真实**阶段，且失败后**立即**可再次提交。

**Independent Test**: 注入各类失败（限流、上游 5xx、鉴权、截断、空产出、内部异常逃逸）→ 验证每一种都在上限内落到明确终态且记录完整、归因正确 → 随后立即再次提交，验证不被拒绝。

### Tests for User Story 3 ⚠️

- [ ] T026 [P] [US3] 在 `app/backend/tests/test_pipeline_recovery.py` 补测试：**内部异常从 `_call_step` 重试 try 之外逃逸**（在 `_get_version` / `_get_step` / `await self._db.commit()` / `GenTxtRequest` 构造处注入异常）→ 断言失败阶段是**流水线实际所在阶段**而非硬编码的 3，且该版本下**不存在**残留 `running` 步骤（FR-014/FR-015/SC-005）

- [ ] T027 [P] [US3] 新建 `app/backend/tests/test_budget_exhaustion.py`：注入一个永不返回的上游 → 断言版本在**预算附近**（而非 6 倍预算）落到 `failed`、`error_type` 为可区分于上游超时的值、`html` 为空、`prompt` 保留，且紧接着再次 `generate` **被受理**（FR-007/FR-009/FR-018/FR-019）

- [ ] T028 [P] [US3] 在 `app/backend/tests/test_pipeline_recovery.py` 补**五类故障的失败记录完整性**测试：限流耗尽（含 `attempts`）、鉴权失败（立即失败且 `attempts == 1`、不做无谓重试）、上游 5xx（含 `upstream_status`）、截断（`error_type == "truncated"`）、空产出；每类均断言 `error` 可读无堆栈、`html` 为空、未产生多余版本（FR-013/FR-016/FR-017/SC-004）

- [ ] T029 [P] [US3] 扩展 `app/backend/tests/test_stale_recovery.py`：构造一个 `running` 且 `updated_at` 陈旧的版本 → 调用 **`GET /projects/{id}/versions/{seq}/steps`** → 断言返回 `failed`、`error_type` **非空**（当前回收路径写入 `None`，是缺陷）、版本数量不变、且该端点调用后同一项目再次 `generate` 被受理（contracts/rest-api.md 变更 1 + 4）

- [ ] T030 [P] [US3] 新建 `app/backend/tests/test_cancel_live_task.py`，运行在**非 `GENERATION_INLINE`** 模式：用 `_PIPELINE_AI_FACTORY` 注入一个**阻塞在 `asyncio.Event` 上**的假上游，使真实后台任务稳定停驻在某阶段，然后调用 `POST /versions/{seq}/cancel`，断言版本、步骤、项目三者都落到 `cancelled`，且 `_RUNNING_TASKS` 中注册的任务被**真正取消**。这是当前 `GENERATION_INLINE=1` 下结构性不可达的路径（FR-032）

### Implementation for User Story 3

- [ ] T031 [US3] 在 `app/backend/services/pipeline.py` 修正 `_run_stages` 的通用 `except Exception` 兜底：把硬编码的 `step_seq=3` 改为**流水线实际所在阶段**（由 `_call_step` 进入时记录的自有状态提供）、补齐 `attempts`，并在 `summary` 中记录**异常类名**以保留可观测性。**不要**把 `_call_step` 中 try 之外的语句移进 try——那会把「我们自己的代码/数据库故障」误分类成「上游错误」而触发无效重试（research.md R-4）

- [ ] T032 [US3] 在 `app/backend/services/pipeline.py` 的 `_fail` 增加**收尾清扫**：把该版本下仍为 `running` 的步骤一并置为 `failed`，已在成功阶段完成的步骤保持 `succeeded`（FR-015/INV-S2）

- [ ] T033 [US3] 在 `app/backend/routers/atoms.py` 为 `GET /projects/{public_id}/versions/{seq}/steps` 补齐 `_recover_stale_versions`（限本人归属，与其它路由一致），并让**回收路径写入非空的 `error_type`**。响应结构不变；`status` 可能因回收由 `running` 变为 `failed`，属期望行为（contracts/rest-api.md 变更 1）

- [ ] T034 [US3] 在 `app/backend/services/aihub.py` 的 `AIHubService.__init__` 为 `AsyncOpenAI(...)` 显式设置 `timeout=STAGE_TIMEOUT` 与 `max_retries=1`，不依赖库默认值——库默认值会与自有 `wait_for` 叠加，使实际等待不可预测（FR-012 / research.md R-9）

- [ ] T035 [US3] 在 `app/frontend/src/pages/Index.tsx` 区分失败文案与提供重试入口：「超出时间预算」（平台侧止损）与「上游不可用」（上游不响应）必须给出**可区分**的说明，失败后展示重试入口并保留原描述，不再出现永久「生成中」（FR-017）

**Checkpoint**: 三个用户故事均可独立验证 —— 失败可理解、可重试、可观测，且无版本会无限期停留「进行中」

---

## Phase 6: Polish & Cross-Cutting Concerns

- [ ] T036 [P] 全量回归：在 `app/backend` 执行 `python -m pytest tests/ -q` 断言 **0 failed / 0 errors**（SC-011），在 `app/frontend` 执行 `npm run lint && npm run build`，两侧结果留档到 `evidence/`

- [ ] T037 [P] 线上证据采集（**消耗真实模型配额**）：依次运行 `app/backend/probe_upstream.py`（判断上游是否仍是瓶颈）与 `app/backend/e2e_two_round.py`（两轮增量 + 旧功能保留 + 会话恢复），输出留档到 `evidence/`（SC-012）

- [ ] T038 [P] 更新 `docs/验收复核报告-生成流水线与归属隔离.md`：逐条标注每个已确认缺陷的修复状态与对应任务号，并补充本次新增的会话侧发现（`Secure` / 登出清 cookie / 恢复上次项目）

- [ ] T039 处置未跟踪的探针产物：`app/backend/e2e_acceptance.py`、`e2e_two_round.py`、`probe_upstream.py` 与 `_det.json`、`_s2.json`、`_s3.json`、`_s4.json`、`_s4b.json`、`_steps.json`、`_v1.json`、`_v2.json`、`_v2b.json`、`_v2c.json`、`_ver.json`——脚本按 `quickstart.md` 的定位纳入版本管理，中间 JSON 转储删除或移入被忽略的目录。同时提示用户：复核期间在线上创建了 3 个测试项目并消耗了模型配额

- [ ] T040 按 `uploads/specs/002-harden-increment-session/quickstart.md` 完整走一遍验证流程，确认指南本身可执行、命令无误、期望值与实现一致

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: 无依赖，可立即开始
- **Foundational (Phase 2)**: 依赖 Setup 完成 —— **阻塞全部用户故事**
- **User Stories (Phase 3~5)**: 均依赖 Foundational 完成，之后三者可并行
- **Polish (Phase 6)**: 依赖所需用户故事全部完成

### User Story Dependencies

- **US1 (P1)**: Foundational 后可开始，**不依赖** US2/US3
- **US2 (P2)**: Foundational 后可开始，**不依赖** US1/US3
- **US3 (P3)**: Foundational 后可开始，**不依赖** US1/US2

> 三者的独立性已刻意保持：US1 的独立测试只需预算可用（Foundational），不需要轮询端点回收（US3 的范围）；US3 的独立测试只需注入口，不需要两轮增量链路。

### 关键任务级依赖

- `T009 → T012`、`T010 → T013`、`T011 → T013`（测试先红，再实现）
- `T014` 依赖 `T003~T006`（预算与自终结就位后，两轮才可能都到终态）
- `T017 → T021 → T022`（登出端点契约 → 后端实现 → 前端调用）
- `T015/T016 → T019`（归属键测试 → `owner.py` 实现）
- `T026 → T031`、`T026 → T032`（归因测试 → 归因与清扫实现）
- `T029 → T033`（回收测试 → 轮询端点补回收）
- `T030` 是**唯一**需要非 INLINE 模式的测试，与其它测试文件无冲突

### Within Each User Story

- 测试必须**先写并确认失败**，再实现
- 归因/清扫（T031/T032）先于端点改造（T033）——否则 T029 的 `error_type` 断言无法满足
- 每个故事完成后立即在其 Checkpoint 处独立验证

### Parallel Opportunities

- Setup：`T001`、`T002` 可并行
- Foundational：`T003~T006` 同处 `pipeline.py`，**必须串行**；`T007`（新测试文件）与 `T008`（既有测试文件）可并行
- US1：`T009/T010/T011` 三个测试任务同处 `test_two_round_increment.py`，**建议串行**以免冲突；`T014` 独立于 `T012/T013`
- US2：`T015~T018` 中 `T015/T016/T018` 同处 `test_owner_dependency.py` 建议串行，`T017` 与 `T025` 是新文件可并行；`T020/T021/T023/T024` 与 `T019` 分属不同文件可并行
- US3：`T027`、`T030` 是新文件可并行；`T028` 与 `T026` 同处 `test_pipeline_recovery.py` 建议串行
- Polish：`T036`、`T037`、`T038` 可并行

---

## Parallel Example: Foundational

```bash
# T003~T006 必须串行（同一文件 pipeline.py）
Task: "T003 声明 GENERATION_BUDGET_SECONDS 并收紧阶段常量"
Task: "T004 接入 deadline / remaining 与预算感知超时"
Task: "T005 实现预算耗尽止损路径"
Task: "T006 删除 _heartbeat 与其常量"

# 以下两个可同时进行
Task: "T007 新建 tests/test_time_budget_invariants.py 断言三个时间常量关系"
Task: "T008 修复 6 个与身份无关的红灯"
```

## Parallel Example: User Story 3

```bash
# 新文件互不冲突，可并行
Task: "T027 新建 tests/test_budget_exhaustion.py"
Task: "T030 新建 tests/test_cancel_live_task.py（非 INLINE 模式）"

# 实现侧：pipeline.py 内两处改动串行，routers/aihub 可并行
Task: "T031 修正通用兜底的失败归因"
Task: "T032 _fail 增加残留 running 步骤清扫"
Task: "T034 services/aihub.py 显式 timeout 与 max_retries"
```

---

## Implementation Strategy

### MVP First（仅 User Story 1）

1. 完成 Phase 1: Setup
2. 完成 Phase 2: Foundational（**关键**——阻塞全部故事）
3. 完成 Phase 3: User Story 1
4. **停下并独立验证**：两轮增量在上限内到终态，且旧功能保留有产物级证据
5. 此时即可部署——因为 US1 依赖的预算与自终结保证同时消灭了「72 分钟最坏耗时」与「永久生成中」两个最严重缺陷

### Incremental Delivery

1. Setup + Foundational → 时间预算与自终结就位（**单独这一步就已经解决了复核中最严重的两个问题**）
2. 加 US1 → 独立验证 → 部署（MVP：核心价值主张成立）
3. 加 US2 → 独立验证 → 部署（"我的东西还在吗"的信任底线成立）
4. 加 US3 → 独立验证 → 部署（失败可理解、可重试）
5. 每个故事独立增加价值，且不破坏前一个

### Parallel Team Strategy

1. 全员共同完成 Setup + Foundational
2. Foundational 完成后：
   - 开发者 A：US1（增量与产物级保留）
   - 开发者 B：US2（会话与身份）
   - 开发者 C：US3（失败归因与回收）
3. 三个故事的文件交集极小（US1 主改 `pipeline.py`，US2 主改 `owner.py` / `atoms.py` / 前端，US3 主改 `pipeline.py` / `atoms.py`）——**注意 US1 与 US3 在 `pipeline.py` 上有交集、US2 与 US3 在 `atoms.py` 上有交集**，并行时需协调

---

## Notes

- [P] 任务 = 不同文件、无未完成依赖
- [Story] 标签用于把任务追溯到 `spec.md` 中的用户故事
- 每个用户故事都应能独立完成与独立验证
- **实现前必须确认测试是红的**
- 每个任务或逻辑分组后提交
- 可在任一 Checkpoint 停下独立验证故事
- 避免：任务描述含糊、同一文件冲突、破坏故事独立性的跨故事依赖

### 本特性特有的三个陷阱

1. **不要把 `_call_step` 中 try 之外的语句移进 try**（T031）——会让数据库/代码故障被误判为可重试的上游错误，浪费预算并掩盖真问题。
2. **不要让心跳以任何形式复活**（T006）——"仅在真实进展时刷新"等价于删除心跳，因为唯一会推进的进展点本来就写 `versions`。
3. **不要为让测试变绿而改被测实现的行为**（T008）——红灯的根因是合并 `bd78a46` 拼接了两条谱系，属于测试侧需要对齐，不是实现侧需要让步。
