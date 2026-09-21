# Implementation Plan: 增量可靠性与会话连续性加固

**Branch**: `002-harden-increment-session` | **Date**: 2026-09-21 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `uploads/specs/002-harden-increment-session/spec.md`

## Summary

本次复核的核心发现是：**这两项验收之所以不通过，不是功能缺失，而是「时间」没有被当作一等公民管理**。

- 生成流水线的第三阶段（代码产出）实测耗时在 126s ~ 1715s 之间波动，而**它没有任何整体时间上限**：恢复链共 6 次模型调用，每次最坏 3 次重试 × 240s，最坏合计约 **72 分钟**，是前端等待上限（12 分钟）的 6 倍。实测第一轮生成在 **1501.6s（25 分钟）** 仍未到达终态。
- 为"防止活任务被误杀"而引入的**心跳，反而废掉了超时回收**：心跳每 30s 无条件续期，而回收判据正是这个续期时间——「进程活着」被当成了「任务有进展」。
- 用户界面轮询的那个接口，**恰好是唯一不具备回收能力的接口**；于是后台任务死掉后，界面会永远显示「生成中」。

技术路线（详见 `research.md`）：**用「自终结 + 硬截止」取代「心跳 + 惰性回收」**。

1. 引入**单一整体时间预算** `GENERATION_BUDGET_SECONDS`，在整个流水线层生效（而非逐次调用生效）；
2. 单次调用超时下调到上游网关实际切断点之内，重试次数收紧；
3. **删除心跳**，改为流水线自我终结 + 硬截止回收，使不变量变得可一句话说清：
   `GENERATION_BUDGET(8min) < STALE_AFTER(10min) < 前端轮询上限(12min)`
4. 轮询端点补齐回收；失败落库补齐归因；会话侧补 `Secure`、登出清 cookie、恢复上次项目。

## Technical Context

**Language/Version**: Python 3.11+（后端 FastAPI + SQLAlchemy async）、TypeScript + React 18（前端 Vite）

**Primary Dependencies**: FastAPI、SQLAlchemy(async)、Pydantic v2、`openai`（AsyncOpenAI）、httpx、pytest + pytest-asyncio；前端 `@metagptx/web-sdk`、Vite

**Storage**: 平台托管关系库（经 `DATABASE_URL`）；测试用内存 SQLite（`tests/conftest.py` 的 `shared_session_maker` 把 `db_manager.async_session_maker` 重绑到内存库）

**Testing**: `cd app/backend && python -m pytest tests/ -q`（必须用 `python -m`，仓库无 conftest/pyproject 于根，靠 CWD 进 sys.path）；前端 `npm run lint` / `npm run build`；线上端到端脚本见 `quickstart.md`

**Target Platform**: 平台网关（约 120s 读超时）+ 单 worker 部署；前端浏览器

**Project Type**: Web application（`app/backend` + `app/frontend`）

**Performance Goals**: 95% 的生成在 4 分钟内到达终态；100% 在 8 分钟内到达终态（spec SC-001）

**Constraints**:
- **总预算 < 回收阈值 < 前端轮询上限**，三者必须构成单一可校验的关系（spec FR-008/SC-010）
- 不可修改 `app/backend/core/**`、`models/**`、`main.py`、`lambda_handler.py`
- 不新建用户表；`routers/` 路由自动发现；所有前缀 `/api/v1/`
- 错误信封恒为 `{"error":{"code","message"}}`，码取 `VALIDATION_ERROR|NOT_FOUND|CONFLICT|UPSTREAM_ERROR|INTERNAL_ERROR`
- 生成必须保持**异步受理**语义（平台网关 120s 读超时）
- 数据库事务不得跨越慢的外部调用

**Scale/Scope**: 单 worker、演示规模；本特性改动集中在 `services/pipeline.py`、`services/aihub.py`、`routers/atoms.py`、`dependencies/owner.py` 与前端 `pages/Index.tsx`、`lib/atoms.ts`

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` 目前仍是**未填写的模板**（`[PROJECT_NAME]`、`[PRINCIPLE_1_NAME]` 等占位符原样存在），因此不存在已批准的项目宪法可供门禁。本计划改以仓库中**实际生效的约束文件**作为门禁来源，并逐条给出通过判定：

| 门禁（来源） | 判定 | 依据 |
|---|---|---|
| 不得修改平台受保护目录（CLAUDE.md / backend README） | **PASS** | 全部改动落在 `services/`、`routers/`、`dependencies/` 与前端 `src/`；**不触碰** `core/**`、`models/**`、`main.py`、`lambda_handler.py` |
| 不新建用户表；用户管理用平台内建（backend README） | **PASS** | 本特性不新增表；身份仍由 `dependencies/owner.py` 服务端派生 |
| 路由前缀必须 `/api/v1/`，错误信封格式固定（backend README） | **PASS** | 唯一的接口新增（登出）沿用 `/api/v1/atoms/...` 前缀与既有信封 |
| 事务不得跨越慢调用（CLAUDE.md 数据库会话边界规则） | **PASS** | 保持既有「短事务夹住 AI 调用」结构；预算检查为纯内存计算 |
| 保持异步受理语义（CLAUDE.md 120 秒约束） | **PASS** | 不引入任何同步等待；`generate` 仍毫秒级返回 202 |
| 取消必须状态优先且晚到结果不得覆盖 `cancelled`（CLAUDE.md） | **PASS** | 保留 `ACTIVE_STATUSES` 全部守卫；预算超时同样受该守卫保护 |
| 沙箱约束（生成物不得依赖 localStorage/fetch/CDN） | **PASS** | 不改动 `CODE_SYSTEM`/`DESIGN_SYSTEM` 的该部分约束 |
| 单一数据模型来源（改 schema 而非改生成的 ORM） | **PASS** | 本计划**不新增列**，规避该约束（见 `research.md` R-3） |

**结论：门禁全部通过，无违规需在 Complexity Tracking 中辩护。**

## Project Structure

### Documentation (this feature)

```text
uploads/specs/002-harden-increment-session/
├── spec.md              # 需求规格（/speckit-specify 输出）
├── plan.md              # 本文件（/speckit-plan 输出）
├── research.md          # Phase 0 输出：设计决策与取舍
├── data-model.md        # Phase 1 输出：实体与状态机
├── quickstart.md        # Phase 1 输出：可复跑的验证指南
├── contracts/           # Phase 1 输出：接口契约
│   ├── rest-api.md
│   └── generation-lifecycle.md
└── tasks.md             # Phase 2 输出（/speckit-tasks 生成，本命令不创建）
```

### Source Code (repository root)

```text
app/backend/
├── dependencies/
│   └── owner.py                      # 身份派生：归属键哈希化、验签加固、告警
├── routers/
│   └── atoms.py                      # 轮询端点补回收；新增登出端点；cookie 安全属性
├── services/
│   ├── pipeline.py                   # 整体预算、预算感知的重试/续写、失败归因、去心跳
│   └── aihub.py                      # 上游客户端显式超时与重试
└── tests/                            # 修复 8 红 2 错；补预算/回收/取消/归属键测试

app/frontend/src/
├── lib/atoms.ts                      # 登出清理、身份双通道
└── pages/Index.tsx                   # 恢复上次项目；超时文案与失败区分
```

## Phase 0: Outline & Research

见 [research.md](./research.md)。其中解决的决策：整体预算的**位置与数值**、心跳的**去留**、硬截止的**存储方式**（规避改表）、失败归因的**实现路径**、`Secure` 标志的**判定方式**、登出清 cookie 的**接口形态**、以及**取消路径的可测试化**。

## Phase 1: Design & Contracts

- [data-model.md](./data-model.md) —— 版本状态机、预算与回收的时间关系、失败记录字段、身份键
- [contracts/rest-api.md](./contracts/rest-api.md) —— 既有端点契约不变部分 + 唯一新增端点 + 失败响应字段
- [contracts/generation-lifecycle.md](./contracts/generation-lifecycle.md) —— 生成生命周期状态机与「谁负责写终态」的责任划分
- [quickstart.md](./quickstart.md) —— 可复跑的验证场景（含线上端到端探针）

## Complexity Tracking

> 本特性门禁全部通过，无违规项需要辩护。

需要说明的是本计划**主动引入的复杂度**及其理由：

| 引入项 | 为什么需要 | 被拒绝的更简方案 |
|---|---|---|
| 单一整体预算（跨阶段截止） | 逐次超时的乘积不受控（6×3×240s）；不设整体预算就无法保证 SC-001/SC-010 | 仅调小 `STAGE_TIMEOUT` 仍无法给出上界 |
| 预算感知的续写裁剪 | 恢复动作必须"花得起"才执行，否则预算被单个阶段吃光 | 固定续写轮次在预算不足时仍会启动 |
| 删除心跳 | 心跳把"存活"误当"进展"，是回收失效的直接原因；有自终结后它反而有害 | 保留心跳并另加截止字段 = 两套时间语义并存 |

## Post-Design Constitution Re-check

Phase 1 设计完成后复核：结论**仍为全部 PASS**。逐条确认：

- 设计未引入新表，未触碰受保护目录，未改错误信封格式，未引入同步等待；
- 新增的唯一端点沿用 `/api/v1/atoms/` 前缀与既有信封；
- 预算与回收判定为纯内存/纯字段比较，不持有跨 AI 调用的事务；
- `ACTIVE_STATUSES` 守卫在预算超时路径上同样生效（晚到的成功不得覆盖已回收的失败，反之亦然）；
- 沙箱相关 prompt 约束未被触碰。

## Open Questions（不阻塞 tasks）

1. **`GENERATION_BUDGET_SECONDS` 的最终定档**：本计划取 480s（8 分钟），依据是「成功样本 238.6s 的约 2 倍冗余」且需满足 `预算 < 回收阈值(600s) < 前端上限(720s)`。若上游持续劣化，需重新定档——但**必须保持三者的不等关系**。
   > **【已定档 · 取 420s，非本处写的 480s】** 实现按 `research.md` R 的定档取
   > `GENERATION_BUDGET_SECONDS = 420.0`（`services/pipeline.py:82`）。
   > 480 也满足 `480 < 600 < 720`，但 420 另有两条好处：① 是成功样本 238.6s 的
   > 约 1.76 倍冗余，仍够用；② 使**单轮**最坏 7 分钟 < SC-001 的 8 分钟门槛，
   > 留 1 分钟余量给回收与轮询。
   > 本处文字保留原文不改写（它是当时的决策记录），差异以本注为准；
   > `quickstart.md` §五 已按 420s 校正读法。
2. **`Secure` 标志的判定**：倾向按请求 scheme 推导（需网关正确传递 `X-Forwarded-Proto`），并提供环境变量覆盖。定档前需确认部署侧是否已启用代理头。
   > **【已落地 · T020，但「确认部署侧代理头」仍未做】** 已按请求实际 scheme 推导
   > 并支持 `ANON_COOKIE_SECURE` 覆盖，直连 TLS 优先（伪造 `X-Forwarded-Proto`
   > 不能把 HTTPS 降级）。**开放项**：线上网关是否确实传递 `X-Forwarded-Proto`
   > 尚无实测证据——若未传，自动推导会退化成「不下发 Secure」，需靠环境变量兜底。
   > 这条与「部署后确认」一并归入 T037 的收尾清单。
3. **上游 524 的根本缓解**（流式/分块产出）不在本特性范围内，但预算数值应对其保持鲁棒。
   > **【仍未做】** 短期缓解已落地（`STAGE_TIMEOUT = 120s < 上游 524 的 126.2s` 断点），
   > 流式/分块本身仍未实现。风险记录见复核报告 §8.2 第 4 条。
