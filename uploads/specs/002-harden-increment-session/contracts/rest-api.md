# Contract: REST API

**Feature**: `002-harden-increment-session` | **Date**: 2026-09-21

前缀恒为 `/api/v1/atoms/`（平台路由自动发现要求）。错误信封恒为 `{"error": {"code", "message"}}`，`code ∈ {VALIDATION_ERROR(400), NOT_FOUND(404), CONFLICT(409), UPSTREAM_ERROR(502), INTERNAL_ERROR(500)}`，`message` 为面向用户的中文、不含堆栈或内部路径。

本文件只记录**契约变化与必须保持的不变量**，不重复既有端点清单。

## 必须保持不变（回归红线）

| # | 不变量 | 理由 |
|---|---|---|
| C-1 | `POST /projects/{id}/generate` 必须**异步受理**：毫秒级返回 `202 {"status":"accepted","version_seq":N}` | 平台网关约 120s 读超时；任何同步等待都会复现最初的 bug |
| C-2 | 仅 `GET /versions/{seq}` 返回 `html`；列表端点步骤 `output` 截断到 2000 字符 | 避免大响应；既有契约 |
| C-3 | 只暴露 `public_id`，内部自增 `id` 永不出现 | 既有契约 |
| C-4 | 一个项目同时最多一个活跃版本；冲突返回 `409 CONFLICT` | 既有契约；但**卡死回收后必须能立即再次提交**（SC-006） |
| C-5 | 取消为**状态优先**：先落库 `cancelled` 并提交，再取消任务 | 既有设计；晚到结果不得覆盖 |
| C-6 | 读路径可见「自己的 + 演示」；写路径仅自己的，演示项目写返回 409（非 404） | 既有归属语义 |

## 变更 1：`GET /projects/{id}/versions/{seq}/steps` 补齐回收

**当前**：该端点是**唯一不调用** stale 回收的路由，而它正是前端每 2.5s 轮询的端点 → 后台任务死亡后界面永久显示「生成中」。

**变更**：在返回前执行与其它路由一致的 stale 回收（限本人归属）。

**契约影响**
- 响应结构**不变**；`status` 可能因回收而由 `running` 变为 `failed`（这是期望行为）。
- 回收后 `error` 必须非空且可读；`error_type` 必须**写入**（当前回收路径写入 `None`，属缺陷，本次一并修复）。
- 幂等：重复调用不得产生副作用或多余版本。

**验收断言**
1. 构造一个 `running` 且 `updated_at` 陈旧的版本 → 调用该端点 → 返回 `failed` 且 `error_type` 非空
2. 回收不得产生新版本；`versions` 数量不变
3. 回收后同一项目再次 `generate` 必须被受理（不再 409）

## 变更 2：新增 `POST /api/v1/atoms/session/logout`

**目的**：清除匿名身份 cookie。因 cookie 为 `HttpOnly`（正确的安全属性），前端原理上无法清除，必须由服务端下发。

**请求**：无请求体。

**响应** `200`：
```json
{ "status": "ok", "anon_key": "<新签发的 nonce.sig>" }
```

**副作用**：`Set-Cookie: atoms_anon=<新值>; Max-Age=180d; HttpOnly; SameSite=Lax; Secure(生产)`；同时删除旧值。

**契约要求**
- 下发**新身份**而非留空，避免"无身份 → 服务端随手签发"的中间态与竞态。
- 必须同时经 cookie 与响应体两条通道下发（与既有双通道一致）。
- 幂等：连续调用各自返回一个新身份，均可用。
- 不得影响任何既有数据归属（只换浏览器侧标识，不迁移、不删除数据）。

**验收断言**
1. 携旧身份调用 → 响应体的 `anon_key` ≠ 旧值；带新身份请求看不到旧身份的项目
2. 旧 cookie 在此响应后失效（服务端已 `delete_cookie`）
3. `Set-Cookie` 在生产（HTTPS）下包含 `Secure`

## 变更 3：匿名 cookie 的安全属性

**当前**：`HttpOnly; SameSite=Lax; Max-Age=180d; Path=/` —— **缺 `Secure`**（设计文档 §S1 明确要求生产启用）。

**变更**：`secure` 由请求实际 scheme 推导（`https` → `Secure`），并提供环境变量覆盖以应对网关未传递 `X-Forwarded-Proto` 的情况。

**验收断言**
1. HTTPS 请求的响应中 `Set-Cookie` 含 `Secure`
2. HTTP 本地请求不含 `Secure`（保证本地开发与测试可回传）

## 变更 4：失败响应的完整性

**当前**：`GET /versions/{seq}` 已返回 `error` 与 `error_type`；但通用兜底路径丢失 `attempts`/`upstream_status`，回收路径写入 `error_type = None`。

**变更**：任何失败路径都必须写入完整的失败记录（字段清单见 `data-model.md`「失败记录字段」）。

**契约要求**
- `error` 面向用户、可读、无堆栈与内部路径。
- `error_type` 必须非空。
- 失败阶段必须**真实**——不得出现"指向未启动阶段"的记录。
- 失败后不得存在仍为 `running` 的步骤。

**验收断言**（对每一类注入故障各跑一次）
1. 限流耗尽 → 记录含 `error_type` 与 `attempts`
2. 鉴权失败 → 立即失败、`attempts == 1`（不做无谓重试）
3. 上游 5xx → 记录含 `upstream_status`
4. 截断 → 记录 `error_type == "truncated"`
5. 内部异常逃逸 → 归因到**真实阶段**，且该版本下无残留 `running` 步骤
6. 上述任一情况：`html` 保持为空，且未产生多余版本

## 变更 5：整体预算耗尽的表现

**新增行为**：流水线超出整体预算时，必须以**可理解的失败**落库，而非继续运行。

**契约要求**
- `error_type` 取一个可区分的值（与"上游超时"区分开，前者是平台侧止损，后者是上游不响应）。
- `error` 文案需说明"本次生成超出时间预算，描述已保留，可重新提交"。
- 用户描述保留；项目立即可再次提交。
- 不得产生新版本。

**验收断言**
1. 注入一个永不返回的上游 → 版本在预算附近（而非 6 倍预算）落到 `failed`
2. 端到端耗时 ≤ 预算 + 容忍度，且严格 < 回收阈值 < 前端等待上限
3. 紧接着再次 `generate` 被受理
