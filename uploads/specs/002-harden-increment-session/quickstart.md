# Quickstart: 增量可靠性与会话连续性加固 · 验证指南

**Feature**: `002-harden-increment-session` | **Date**: 2026-09-21

本文件是**验证/运行指南**，不是实现文档。它回答一个问题：**怎么证明这次改动真的解决了问题**。

术语与字段含义见 [data-model.md](./data-model.md)；接口断言见 [contracts/rest-api.md](./contracts/rest-api.md)；时间约束与责任矩阵见 [contracts/generation-lifecycle.md](./contracts/generation-lifecycle.md)。

## 前置条件

| 项 | 要求 |
|---|---|
| Python 虚拟环境 | `app/backend/.venv`（由 `app/start_app_v2.sh` 创建） |
| 后端测试 | **必须** `cd app/backend && python -m pytest tests/ -q`（仓库根无 conftest/pyproject，靠 `python -m` 把 CWD 放进 `sys.path`） |
| 用哪个 python | 用 venv 里的解释器：Windows 下 `app/backend/.venv/Scripts/python.exe -m pytest …`。**系统 python 通常没装 pytest**，直接 `python -m pytest` 会报「No module named pytest」而不是测试失败 |
| 前端检查 | `cd app/frontend && npm run lint && npm run build` |
| 线上端到端 | 需要 `APP_AI_KEY` 有效的部署；会产生**真实模型调用与额度消耗** |

> ⚠️ **线上验证会消耗真实配额。** 复核期间已观测到单次生成占用上游 126s ~ 1715s。运行前请确认这是可接受的成本。

## 一、本地回归（零成本，每次改完必跑）

```bash
cd app/backend && python -m pytest tests/ -q
```

**期望**：`0 failed, 0 errors`，当前实测 **`157 passed`**（spec SC-011 / FR-035）。

历史对照：本特性开工时的红基线是 `8 failed / 90 passed / 2 errors`，逐字留档在
`evidence/baseline-tests.txt`。它存在的意义是让「转绿」可被核对，而不是被宣称。

重点确认以下测试从红转绿（它们是 `owner.py` 回归的探测器）：

```bash
cd app/backend && python -m pytest tests/test_owner_dependency.py -v
```

**期望**：含 `test_distinct_long_subjects_yield_distinct_keys`（归属键长度无关唯一）与 `test_missing_secret_warns_once` 在内全部通过。

## 二、时间预算与回收（本地注入，零成本）

这类验证靠**故障注入**而非真实等待——把预算相关常量临时调小即可在秒级验证。

```bash
cd app/backend && python -m pytest tests/test_budget_exhaustion.py -v      # 预算耗尽止损
cd app/backend && python -m pytest tests/test_time_budget_invariants.py -v # 常量不等关系
cd app/backend && python -m pytest tests/test_stale_recovery.py -v         # 陈旧回收（含轮询端点）
cd app/backend && python -m pytest tests/test_pipeline_recovery.py -v      # 五类故障 + 归因
cd app/backend && python -m pytest tests/test_cancel_live_task.py -v       # 非 INLINE 模式
```

**必须覆盖的断言**（完整清单见 `contracts/generation-lifecycle.md`「责任矩阵」与 `contracts/rest-api.md` 变更 1/5）：

1. 注入永不返回的上游 → 版本在**预算附近**落地失败，而**不是** N 倍预算；且 `html` 为空
2. 注入的失败使版本落地后，该项目**立即**可再次 `generate`（不再 409）
3. 构造陈旧 `running` 版本 → 调用 `GET /versions/{seq}/steps` → 返回 `failed` 且 `error_type` 非空、版本数不变
4. 五类故障各注入一次，断言失败记录的**完整性**与**归因正确性**（不得指向未启动阶段，不得残留 `running` 步骤）
5. 非 INLINE 模式下调用 `/cancel` → 版本/步骤/项目三者皆为 `cancelled`，且注册的任务被真正取消
6. 断言常量关系 `STAGE_TIMEOUT < GENERATION_BUDGET_SECONDS < STALE_AFTER < 前端轮询上限`

**期望耗时**：全部在分钟级内完成（靠注入而非真等）。
实测：上面五个文件合计 **38 passed / 6.06s**（`test_cancel_live_task.py` 用
`asyncio.Event` 停驻后台任务后主动释放，不真等超时）。

> **两个易踩的坑**（都由 `test_budget_exhaustion.py` 的注释留档）：
> ① 缩小预算时必须**同时**缩 `GENERATION_BUDGET_SECONDS` / `STAGE_TIMEOUT` /
> `MIN_CALL_BUDGET_SECONDS` 三者，只改一个会让另一个成为实际生效的上限，
> 断言就测不到目标路径；② 别用「请求是否在 N 秒内返回」判断受理是否异步——
> `ASGITransport` 上超时取消可能被应用吞掉，断言会空转。

## 三、前端会话与一致性（本地）

```bash
cd app/frontend && npm run lint && npm run build
```

> ⚠️ **`npm run build` 不做类型检查。** `package.json` 里是 `"build": "vite build"`，
> esbuild 只会**剥离**类型而不报错，与仓库 `CLAUDE.md` 写的「tsc + vite build」不符。
> 要真正验证类型，必须另跑 `npx tsc --noEmit`（本次改动后该命令 exit 0）。
> 建议把 `tsc --noEmit` 并入 `build`，否则类型错误可以静默进产物。

**手工验证清单**（浏览器）：

1. 创建一个项目并生成 → 刷新页面 → 项目与版本仍在，页面内容与摘要前缀与刷新前一致
2. 生成进行中刷新 → 界面**自动接回**并继续显示进展
3. 打开 A 项目后刷新 → 回到 A 项目（而非空白工作区）
4. 登出 → 浏览器侧不再残留上一身份痕迹（Network 面板确认后续请求不带旧 cookie）
5. 无痕窗口访问 → 看不到自己的项目（只见演示项目）

## 四、线上端到端（真实成本）

> ⛔ **本节必须在「包含本特性修复的构建已部署」之后执行。**
> 两个脚本都把 `BASE` 硬编码为 `https://keegan.pub.atoms.world`，只能在线上运行。
> 若本分支尚未部署，跑它们验证的是**平台上一次发布的旧构建**——结论与本次改动无关，
> 却照样消耗真实模型配额并创建真实公开项目。故此节当前标注为**待部署后执行**
> （任务 `T037`），而不是「跳过」。
>
> 另：脚本外连上游，用 venv 解释器跑（`app/backend/.venv/Scripts/python.exe probe_upstream.py`），
> 系统 python 缺 `httpx` 等依赖。

### 4.1 上游隔离探针（判断"是上游还是我们"）

`app/backend/probe_upstream.py` 用**与线上完全相同**的 prompt 构造分别打三个阶段，用来区分「上游拒绝了某个阶段的载荷」与「流水线自身代码问题」。

```bash
cd app/backend && python probe_upstream.py
```

**历史观测**：阶段1 ≈ 4.0s / 200；阶段2 ≈ 9.8s / 200；阶段3 ≈ **126.2s / HTTP 524**。改动后阶段3 的表现是判断上游是否仍是瓶颈的关键。

### 4.2 两轮增量 + 旧功能保留 + 会话恢复

`app/backend/e2e_two_round.py` 对部署执行：建立匿名身份 → 新会话（仅 header）复访 → 跨身份隔离 → 建项目 → 轮次1 从零生成 → 轮次2 指代型增量 → 断言长度比、旧功能逐项保留、摘要自洽、旧版本不可变 → 新会话取回。

```bash
cd app/backend && python e2e_two_round.py
```

**必须满足**（对应 spec SC-001/002/007/008）：

| 断言 | 期望 |
|---|---|
| 两轮都在上限内到达终态 | ✅（当前：轮次1 在 1501.6s 仍未终结） |
| 长度比 `seq2/seq1` | ≥ 0.80 |
| 旧功能逐项保留 | 全部为 `True` |
| 第二轮新增能力存在 | `True` |
| 摘要自洽 / 以 `</html>` 结尾 / 单 `<body>` / 单 `<!DOCTYPE` | 全部成立 |
| 第二轮后回取第一轮版本 | 摘要与内容**逐字节不变** |
| 新会话（仅 header）复访 | 200 且能看到自己的项目 |
| 全新身份 | **看不到**探针项目 |

**历史观测**：`anon_cookie_match: true`；`new_session_via_header: 200`；`new_identity_sees_probe_project: false`（隔离正确）；**`round1: TIMEOUT`（1501.6s）**——这正是本次要修的核心问题。

## 五、通过标准

一项验收算通过，必须**同时**满足：

1. 本地套件 `0 failed, 0 errors`；
2. 时间预算、回收、归因、取消四类断言全部覆盖且通过；
3. 线上两轮增量**双双在 8 分钟内到达终态**（spec SC-001），且第二轮**产物层**保留第一轮全部能力（SC-002）；
   > SC-001 的读法以 `research.md` 的定档为准：`GENERATION_BUDGET_SECONDS = 420s`
   > 使**单轮**最坏 7 分钟 < 8 分钟，故「每一轮都在 8 分钟内到达终态」成立。
   > 注意 `plan.md` 的「定档」一节写的是 480s，与 `research.md` 和实现（420s）不一致；
   > 实现取的是更严的 420s（`plan.md` 那句需修正，见 §注）。
4. 会话恢复两条通道（cookie / header）与跨身份隔离均通过（SC-007/008）；
5. 存在**可复跑**的证据留档（脚本输出或日志），而非一次性手工观察（SC-012）。

## 六、证据留档约定

在 `uploads/specs/002-harden-increment-session/evidence/` 下按日期留档，文件名形如
`<日期>-<被测版本>-<场景>.txt`（详见该目录 `README.md`）。这样"源码/预览一致性"
与"旧功能保留"这类断言就不再依赖人工观察。

现有留档：

| 文件 | 内容 |
|------|------|
| `baseline-tests.txt` | 开工时的红基线（8 failed / 90 passed / 2 errors），转绿的对照物 |
| `2026-09-21-local-phase2-foundational.txt` | 基础设施阶段（预算常量、截止时刻） |
| `2026-09-21-local-us1-increment.txt` | US1 两轮增量与上下文体积 |
| `2026-09-21-local-us2-session.txt` | US2 身份与登出（含 `monkeypatch` 缓存踩坑记录） |
| `2026-09-21-local-us3-budget-cancel.txt` | US3 预算/取消 + 全量回归 + 受控变异表 |

每份都记录了当轮的 TDD 红→绿过程、**受控变异**（故意改坏实现看测试是否变红，用以
证明断言不是自证；每次变异后逐字回滚，`grep -rn MUTANT` 为空）与原始命令输出。

> 注意：`docs/验收证据/` 在原设计文档中被提及但**并不存在**，属既有缺口；
> 本特性按上面的命名约定统一落在 `evidence/`，未新建平行目录。
> 线上部分（探针、两轮端到端、真实回滚、贪吃蛇复测）的留档**仍缺**，待部署后补（T037）。
