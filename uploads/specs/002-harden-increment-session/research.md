# Phase 0 Research: 增量可靠性与会话连续性加固

**Feature**: `002-harden-increment-session` | **Date**: 2026-09-21

本文件记录每项设计决策的**选择、理由与被否决的方案**。所有数值均来自 2026-09-21 对 `https://keegan.pub.atoms.world` 的实测。

## 实测数据基线（决策依据）

| 观测项 | 数值 |
|---|---|
| 阶段1 需求分析 | 22.5s / 22.9s / 23s（多次复测稳定） |
| 阶段2 结构设计 | 17.6s / 18s / 18.7s（稳定） |
| 阶段3 代码生成 · 成功样本 | **203.96s**（该次流水线总计 238.6s） |
| 阶段3 · 上游切断样本 | **126.2s → HTTP 524**（Cloudflare 网关切断） |
| 阶段3 · 未终结样本 | **996s / 1331s / 1715s**（仍在运行） |
| 轮次1 端到端未达终态 | **1501.6s（25 分钟）超时** |
| 版本停在 `running` 无终态 | **14.7 分钟**（直到触发回收的端点被调用） |
| 当前阶段3 最坏理论值 | 6 次调用 × (3 次重试 × 240s + 退避) ≈ **4329s ≈ 72 分钟** |
| 前端等待上限 | 12 分钟（`GENERATION_POLL_TIMEOUT_MS`） |
| 平台网关读超时 | 约 120s |

---

## R-1 · 整体时间预算的位置与数值

**Decision**: 引入 **`GENERATION_BUDGET_SECONDS = 420`（7 分钟）**，语义是**整条流水线**从开始到终态的总预算（覆盖阶段1+2+3 与其全部重试、续写、重跑），以实现为**从流水线起点计算的单调截止时刻**，逐次调用取 `min(STAGE_TIMEOUT, remaining)`。

同时收紧单次调用参数：

| 常量 | 现值 | 新值 | 理由 |
|---|---|---|---|
| `STAGE_TIMEOUT` | 240.0 | **120.0** | 上游网关在 **126.2s** 就切断（524）。把单次等待设在上游切断点之内，避免为一个已被丢弃的请求白等 114s |
| `MAX_ATTEMPTS` | 3 | **2** | 重试的意义在预算内才有价值；3 次重试的乘积已超出总预算 |
| `RETRY_BACKOFF_SECONDS` | (0.5, 1.0, 2.0) | **(0.5, 1.0)** | 与 `MAX_ATTEMPTS=2` 对齐 |
| `GENERATION_BUDGET_SECONDS` | 不存在 | **420.0** | 见下 |

**Rationale**:
- 420s 的取值同时满足三个约束：① 是成功样本 238.6s 的约 1.76 倍冗余；② 保证 `420 < STALE_AFTER(600s) < 前端上限(720s)` 的不等关系；③ 使得「整条流水线 ≤ 7 分钟」成立，从而满足 spec SC-001 的「100% 在 8 分钟内到达终态」。
- 预算必须**整体生效**而非逐次生效——这是 spec FR-007 的核心，也是当前设计最大的漏洞：6 次调用各自都有 721.5s 的上限，但没有任何一层管住它们的**乘积**。

**Alternatives considered**:
- *只调小 `STAGE_TIMEOUT`*：拒绝。无论单次超时调到多小，6 次调用的乘积仍然无界，给不出上界，无法满足 SC-010。
- *只限制续写轮次*：拒绝。截断了恢复能力但没给出上界，且成功路径也会被牵连。
- *把预算设在单阶段层*：拒绝。阶段3 会吃光预算并饿死阶段1/2 的重试机会；预算必须全局。

---

## R-2 · 心跳的去留

**Decision**: **删除 `_heartbeat`**，并删除 `HEARTBEAT_INTERVAL`。

**Rationale**: 心跳是本次"无限卡死"的直接成因。它的初衷（见 `pipeline.py` 注释与设计文档）是防止**活着的长任务被误杀**；但它采用了"无条件续期 `versions.updated_at`"的实现，而回收判据恰好就是 `updated_at`——于是语义从「任务有进展」滑成了「进程还活着」。观测到的 14.7 分钟无终态正是这个滑移的结果。

删除它之所以安全，是因为 **R-1 给出了自终结保证**：流水线一定在 420s 内自己写下终态。因此：

- 活任务的最长"静默期" = 阶段3 的预算 420s（阶段1/2 只占约 40s）< `STALE_AFTER` 600s → **永远不会被误杀**；
- 进程死亡时 `updated_at` 自然冻结 → 600s 后被正确回收。

**关键校验**：`versions.updated_at` 只在**真实进展点**推进（`_finish_step` 写 `summary`、`_fail` 写终态）。`_call_step` 把步骤置 `running` 时改的是 `generation_steps` 行，**不会**推进 `versions.updated_at`。因此最大静默间隔 = 阶段3 的 420s，余量 180s。

**Alternatives considered**:
- *保留心跳，改为"仅在真实进展时刷新"*：等价于删除心跳（因为唯一会推进的进展点本来就写 `versions`），却多保留了一个需要解释的机制。
- *保留心跳 + 另加硬截止字段*：拒绝。两套时间语义并存，未来必然再次出现"哪个说了算"的歧义，且需要改表（见 R-3）。

---

## R-3 · 硬截止的存储方式（规避改表）

**Decision**: **不新增任何数据库列，不改 `data_models/` schema。** 硬截止**不落库**，改为"自终结 + 既有 `updated_at` 惰性回收"的组合。

**Rationale**:
- 平台约束规定 `models/**` 由 `data_models/*.json` 生成、不得直接编辑；新增列意味着改 schema 并重新生成 ORM，成本与风险都高于本特性的收益。
- R-1 + R-2 已经使既有 `updated_at` 语义**重新变得正确**：它能同时表达"最后一次真实进展"，从而同时支撑"活任务不被误杀"与"死任务被回收"两个目的。
- 硬截止的**执行**由流水线自己在内存中完成（`asyncio.wait_for` 包住整条链），不需要持久化即可保证有界。

**Alternatives considered**:
- *新增 `versions.deadline_at` 列*：更直白，但需改 `data_models/versions.json` 并重新生成 `models/versions.py` 与迁移。**记录为长期更优解**，待确有跨进程/多 worker 需求时再引入（多 worker 下"任务是否活着"无法再由进程内状态判断，届时该列是必需品）。
- *把截止时间塞进 `versions.summary` 的保留键*：拒绝。`summary` 是面向用户的步骤摘要（会被 API 直接返回），混入内部控制字段会污染契约。

---

## R-4 · 失败归因的实现路径

**Decision**: 两处修改：
1. `_run_stages` 的通用 `except Exception` 兜底，把硬编码的 `step_seq=3` 改为**流水线实际所在阶段**（由 `_call_step` 在进入时记录的自有状态），并补齐 `attempts`；同时在 `summary` 中记录**异常类名**以保留可观测性。
2. `_fail` 增加**收尾清扫**：把该版本下仍处于 `running` 的步骤一并置为 `failed`。

**Rationale**: 线上那次失败呈现 `error_type=unknown`、无 `attempts`、`steps=['succeeded','running','failed']`，与通用兜底分支精确吻合。推理链是：`_call_step` 抛 `PipelineError` 时**必带** `attempts`；只有通用兜底不带。因此异常是从 `_call_step` 的**重试 try 之外**逃逸的，可疑点明确且都在同一函数内：

```python
await self._get_version(...)      # try 之外
await self._get_step(...)         # try 之外
await self._db.commit()           # try 之外
request = GenTxtRequest(...)      # try 之外
```

其后果有两层，都需要修：**错误归因到未启动的阶段**，以及**已完成的阶段被留在 `running`**（第 2 点修复）。

不把这几条语句移进 try（那会把"我们自己的代码/数据库故障"误分类成"上游错误"而触发无效重试），而是让兜底分支**说真话**：它是一个内部错误，就应该以内部错误的形态落库。

**Alternatives considered**:
- *把 try 之外的语句包进去*：拒绝。会让 DB 故障被 `classify_upstream_error` 误判为可重试的上游错误，浪费预算并掩盖真正的问题。
- *只改文案，不改归因*：拒绝。归因错误会误导排障方向（观测到的正是"以为是阶段3，实际是阶段2 期间逃逸"）。

---

## R-5 · `Secure` 标志的判定方式

**Decision**: `secure` 由**请求实际 scheme** 推导，并提供环境变量覆盖：`secure = env("ANON_COOKIE_SECURE") 判定 or request.url.scheme == "https"`。

**Rationale**: 设计文档 §S1 明确要求 `HttpOnly; SameSite=Lax; Secure(生产); Max-Age=180d`，当前实现缺 `Secure`。按 scheme 推导可在本地 HTTP 开发与生产 HTTPS 之间自动切换，无需为环境差异硬编码开关；环境变量覆盖用于网关未正确传递 `X-Forwarded-Proto` 时兜底。

**Alternatives considered**:
- *恒为 `Secure=True`*：拒绝。会让本地 HTTP 开发与测试环境的 cookie 无法回传，破坏既有测试与开发流程。
- *按环境名硬编码（如 `APP_ENV == "production"`）*：拒绝。多引入一个必须正确设置的环境变量，且与"实际传输是否加密"这一真实条件脱钩。

---

## R-6 · 登出清 cookie 的接口形态

**Decision**: 新增 **`POST /api/v1/atoms/session/logout`**：删除匿名 cookie 并**签发一个全新的匿名身份**，把新标识经 `Set-Cookie` 与响应体一并返回（沿用既有双通道下发）。前端 `logout()` 在调用平台登出后调用它，并继续清 localStorage。

**Rationale**: cookie 是 `HttpOnly` 的，这是**正确的安全属性**（防 XSS 窃取），因此前端**在原理上就清不掉它**——只能由服务端下发删除指令。这正是当前"登出后仍会回到旧匿名身份"的根因：`clearAnonKey()` 只清了 localStorage，而认证优先级里 cookie 高于请求头。

下发新身份而非留空，是为了让登出后的第一个请求就带着一个干净身份，避免"无身份 → 服务端随手签发 → 响应体与 cookie 竞态"的中间态。

**Alternatives considered**:
- *去掉 `HttpOnly` 让前端能清*：拒绝。用安全属性换便利性，方向错误。
- *服务端仅删除 cookie 不下发新身份*：可行但会引入一次额外往返与竞态，不如一次请求同时完成"清旧 + 立新"。

---

## R-7 · 取消路径的可测试化

**Decision**: 新增独立的取消路径测试模块，运行在**非 INLINE** 模式下：通过 `_PIPELINE_AI_FACTORY` 注入一个**阻塞在 `asyncio.Event` 上**的假上游，使真实后台任务稳定停驻在某个阶段，然后调用 `POST /versions/{seq}/cancel`，断言版本、步骤、项目三者都落到 `cancelled`，且 `_RUNNING_TASKS` 中注册的任务被真正取消。

**Rationale**: 当前 `GENERATION_INLINE=1` 使 `generate` 在当前协程内跑完流水线，因此**没有后台任务可取消**——`/cancel` 路由与 `task.cancel()` 路径**结构性不可达**，这正是 spec FR-032 所指的证据缺口。需要一个能让后台任务"停住"的确定性手段，`asyncio.Event` 是标准且无额外依赖的做法。

**Alternatives considered**:
- *用 `sleep` 制造时间窗*：拒绝。时序脆弱，CI 上必然不稳定。
- *直接对 `_start_generation` 做单元测试*：不足。绕过了路由层的状态优先写入顺序，而"状态优先"正是该设计的关键不变量。

---

## R-8 · 归属键的哈希化

**Decision**: 恢复 `d53420b` 的写法——登录身份归属键取 `sha256(subject)[:32]`，总长 **37**（`user:` 5 + 32），远小于 `owner_key` 的 64 上限；恢复 `_SUBJECT_CHARS = 32` 常量与"密钥缺失时告警一次"的行为。

**Rationale**: 现写法 `f"user:{sub}"[:64]` 在本地已实证碰撞——3 个不同 `sub`（长度 80 / 60 / 60，前缀相同）产出**同一个** `owner_key`，即**不同登录用户共享归属键，可互见数据**。而 `owner_key` 列宽 64 是既定 schema，数据库不会拦截这种截断，因此必须在派生层解决。

**Alternatives considered**:
- *扩宽 `owner_key` 到 255*：拒绝。需改 schema 与生成的 ORM，且仍不解决"标识长度无上界"的根本问题；哈希化是长度无关的正解。
- *保留截断但加唯一性校验*：拒绝。校验只能在写入时发现冲突，无法在派生时避免，且引入失败路径。

---

## R-9 · 上游客户端的显式超时与重试

**Decision**: `AsyncOpenAI(..., timeout=STAGE_TIMEOUT, max_retries=1)`，显式声明，不依赖库默认值。

**Rationale**: 设计文档 §3.2② 早已要求（原文 `timeout=180.0, max_retries=1`），但当前实现两者皆无。库默认值会与我们的 `wait_for` 叠加，使实际等待时间不可预测。取 `timeout=STAGE_TIMEOUT` 使其与 R-1 的自有超时一致；`max_retries=1` 保留一次库级重试（对瞬时连接错误有效），且因为 `wait_for` 包在**整次调用之外**，库级重试同样受预算约束，不会造成预算外放大。

**Alternatives considered**:
- *`max_retries=0`，重试完全由自有层负责*：也可行且更可控，但放弃了库对连接抖动的内建处理。取 `1` 与既有设计文档一致。
- *`timeout` 取 180s*：拒绝。新的单次上限是 120s，客户端超时必须与之对齐，否则 `wait_for` 先触发而库仍在后台等待，造成连接泄漏。

---

## R-10 · 「源码 / 预览一致」的可验证化

**Decision**: 用**独立于产出方自述**的三方对照取代自证测试：① 后端 `GET /versions/{seq}` 返回的 `html` 与 `html_sha256`；② 对返回的 `html` 由**验证方独立**计算摘要（不使用被测代码路径）；③ 前端展示条（版本号 / 体积 / 摘要前缀）与代码视图所依据的**同一个数据源**。三者在同一次运行中断言相等。

**Rationale**: 现有测试用同一公式复算同一份响应体，属于自证——它能发现"响应体被改动"，但发现不了"前端展示的其实不是这份数据"或"两个视图取自不同来源"。spec FR-033/SC-007 要求的是后者。

**Alternatives considered**:
- *引入浏览器自动化做像素级比对*：拒绝。超出本特性范围且引入重依赖；本轮只需要证明"两个视图与 API 同源且内容未被篡改"。

---

## 未解决项（明确记录，不假装已解决）

1. **上游 524 的根本缓解**（流式 / 分块产出）不在本特性范围。本特性的预算是**症状层面的止损**：它保证用户拿到终态，但不提高大页面的成功概率。
2. **`CODE_MAX_TOKENS = 16384` 是否偏高**：实测显示长产出更容易撞上上游切断点。是否下调需在预算落地后另行度量——下调会提高截断率，反而增加续写轮次，不能凭直觉决定。
3. **`GENERATION_BUDGET_SECONDS` 的最终定档**：见 plan.md 的 Open Questions。
