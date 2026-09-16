# Implementation Plan: 智能体驱动的应用生成平台（Atoms Demo）

**Branch**: `001-atoms-demo` | **Date**: 2026-09-15 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-atoms-demo/spec.md`

## Summary

构建一个平台型 Web 应用：用户用自然语言描述想要的软件，由**三阶段智能体流水线**（需求分析 → 结构设计 → 代码生成）产出**单个自包含页面**，并在**沙箱 iframe** 中即时渲染为可交互界面。生成过程通过 **SSE 流式推送**，前端实时展示智能体的工作步骤。

技术方案：Vue 3 + TypeScript 前端，FastAPI + SQLAlchemy + MySQL 8 后端，DeepSeek 作为生成模型，单机 Nginx 反向代理部署于腾讯云轻量·香港节点。

## Technical Context

**Language/Version**: Python 3.12（后端）、TypeScript 5.x / Vue 3.4（前端）

**Primary Dependencies**: FastAPI、SQLAlchemy 2.x、PyMySQL、Pydantic v2、httpx、openai SDK（兼容 DeepSeek）；Vue 3、Vite、Pinia、Monaco Editor

**Storage**: MySQL 8，仅监听 `127.0.0.1`，字符集 `utf8mb4`

**Testing**: pytest（后端）、Vitest（前端）——聚焦高风险纯逻辑，不做全面覆盖

**Target Platform**: Linux（Ubuntu 22.04，腾讯云轻量·香港 2vCPU/4GB）+ 现代桌面浏览器

**Project Type**: Web application（frontend + backend）

**Performance Goals**: 首个可见反馈 < 3s；SSE 首字节 < 3s；完整生成 < 90s

**Constraints**: 单机 2vCPU/4GB；生成内容必须在无宿主机权限的沙箱中渲染；MySQL 不得对公网开放；API Key 不得进入公开仓库

**Scale/Scope**: 个位数至数十并发；MVP 约 4 张表 / 6 个接口 / 5 个核心前端组件

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

⚠️ **`.specify/memory/constitution.md` 当前仍是未填写的模板占位符**——项目尚未运行 `/speckit-constitution`。因此**不存在经批准的章程门禁**可供校验。

为使计划仍有约束力，本计划自设四条门禁并声明符合性（建议后续正式化进章程）：

| 门禁 | 内容 | 符合性 |
|---|---|---|
| **G1 安全隔离** | LLM 生成内容必须在无宿主机权限的沙箱中渲染 | ✅ `sandbox="allow-scripts allow-forms"` + CSP，**不含** `allow-same-origin` |
| **G2 凭据保护** | API Key 仅存于 `.gitignore` 覆盖的 `config.py` 或环境变量，绝不进入公开仓库 | ✅ 交付 `config.example.py` + `.gitignore`；提交前须核验 |
| **G3 数据不丢失** | 已生成项目在刷新与服务重启后完整可恢复 | ✅ 全部状态落 MySQL，无内存态依赖 |
| **G4 复杂度克制** | 不实现 spec 未确认的功能 | ✅ 用户权限体系、多人协作、代码导出均已在 spec 中排除 |

**Phase 1 后复评结果**：见文末「Post-Design Constitution Re-check」。

## Project Structure

### Documentation (this feature)

```text
specs/001-atoms-demo/
├── plan.md                      # 本文件
├── spec.md                      # 功能规格
├── research.md                  # Phase 0 输出：技术决策与依据
├── data-model.md                # Phase 1 输出：数据模型
├── quickstart.md                # Phase 1 输出：验证指南
├── checklists/
│   └── requirements.md          # 规格质量清单
├── contracts/
│   ├── rest-api.md              # REST 接口契约
│   └── streaming-events.md      # SSE 事件契约
└── tasks.md                     # Phase 2 输出（由 /speckit-tasks 生成）
```

### Source Code (repository root)

```text
backend/
├── app/
│   ├── main.py                  # FastAPI 应用入口、CORS、静态托管
│   ├── config.py                # ⚠️ 本地配置，含 API Key，.gitignore 覆盖
│   ├── config.example.py        # 提交用的模板，Key 留空
│   ├── db.py                    # SQLAlchemy engine / session
│   ├── models.py                # ORM 模型（4 张表）
│   ├── schemas.py               # Pydantic 请求/响应模型
│   ├── routers/
│   │   ├── projects.py          # 项目与版本读取接口
│   │   └── generate.py          # SSE 生成接口
│   └── services/
│       ├── llm.py               # DeepSeek 客户端封装（流式）
│       ├── pipeline.py          # 三阶段智能体编排
│       ├── prompts.py           # 各阶段提示词
│       └── html_extract.py      # 生成结果提取与净化（纯函数，重点测试对象）
├── tests/
│   ├── test_html_extract.py
│   ├── test_pipeline.py
│   └── test_api.py
└── requirements.txt

frontend/
├── src/
│   ├── main.ts
│   ├── App.vue
│   ├── api/
│   │   ├── client.ts            # REST 调用
│   │   └── stream.ts            # SSE 帧解析（fetch + ReadableStream）
│   ├── stores/
│   │   └── workspace.ts         # Pinia：当前项目、生成状态、步骤
│   ├── components/
│   │   ├── PromptInput.vue      # 描述输入与提交
│   │   ├── AgentSteps.vue       # 智能体步骤可视化
│   │   ├── PreviewPane.vue      # 沙箱 iframe 渲染
│   │   ├── CodeViewer.vue       # Monaco 只读代码视图
│   │   └── ProjectList.vue      # 历史项目
│   └── views/
│       └── Workspace.vue        # 主工作区布局
├── package.json
└── vite.config.ts

deploy/
├── docker-compose.yml           # nginx + api + mysql
├── nginx.conf                   # TLS 终止、静态托管、/api 反代
├── Dockerfile.api
└── .env.example
```

**Structure Decision**: 采用 **Web application** 结构（`backend/` + `frontend/`）。前端构建产物由 Nginx 直接托管，FastAPI 仅处理 `/api/*`，避免跨域并简化部署。三阶段流水线、提示词、LLM 客户端、HTML 提取分列于 `services/` 下，其中 `html_extract.py` 设计为**无副作用的纯函数模块**，是本项目最值得单元测试的部分。

## Complexity Tracking

> 本计划无章程违规项需辩护（章程尚未批准）。以下为**主动记录的复杂度取舍**：

| 决策 | 为何需要 | 更简单的替代方案为何被否 |
|---|---|---|
| 三阶段 Agent 流水线（3 次 LLM 调用） | spec P4 要求"看见智能体在工作"；单次调用无法呈现分阶段进展 | 单次调用出 HTML 最快，但丢失产品的核心体验差异与创新性得分点 |
| SSE 而非轮询 | 生成耗时 40-90s，轮询要么延迟高要么请求量大 | 轮询实现更简单，但无法满足 SC-002"3 秒内首个反馈"与过程可视化的实时性 |
| 独立 `html_extract` 模块 | LLM 输出格式不稳定，提取失败等于整个产品失效 | 内联在流水线中更省代码，但无法独立测试，且失败点难以定位 |

## Post-Design Constitution Re-check

Phase 1 设计完成后复评（详见 [data-model.md](./data-model.md) 与 [contracts/](./contracts/)）：

| 门禁 | 复评结论 |
|---|---|
| **G1 安全隔离** | ✅ `contracts/streaming-events.md` 明确了 `html` 字段为不可信输入；前端 `PreviewPane` 固定使用不含 `allow-same-origin` 的沙箱属性，不提供动态覆盖入口 |
| **G2 凭据保护** | ✅ `config.py` 与 `deploy/.env` 均纳入 `.gitignore`；`docker-compose.yml` 通过环境变量注入，不含明文 |
| **G3 数据不丢失** | ✅ 四张表覆盖项目/版本/消息/步骤；生成中途失败时 `versions.status='failed'` 保留记录，不产生悬空数据 |
| **G4 复杂度克制** | ✅ 未引入 spec 之外的能力；版本回滚复用 `versions` 表，未新增结构 |

**结论**：四条自设门禁全部通过，无违规项，可进入 `/speckit-tasks`。

## Open Questions（不阻塞 tasks）

1. **域名与 TLS**：是否购买低价域名并配置 Let's Encrypt？（详见 research.md R8）
2. ~~**DeepSeek 模型 ID**~~ ✅ **已于 2026-09-15 实测解决**——`deepseek-chat` 可用（遗留别名 → `deepseek-flash` 非思考模式），单次生成 6.8–13 s。⚠️ 注意 `deepseek-flash` 直连**不可用**（token 被 reasoning 吃光）。详见 research.md R4。
3. **`max_tokens` 取值**：代码生成阶段建议 16000，需在实现时写入配置并验证不会截断
