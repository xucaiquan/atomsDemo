# Project Summary

Atoms Studio 是面向应用原型和业务页面生成的智能开发平台。用户通过自然语言描述需求，系统自动分析需求、设计页面并生成完整 HTML，在隔离沙箱中预览可交互应用。项目、版本、生成步骤和对话记录会持久化保存，支持历史版本恢复、持续迭代、源代码查看和回滚。

系统采用异步多阶段生成流程，支持历史上下文、指代消解、增量生成、代码续写、整篇重跑、HTML 校验、版本切换、取消、失败展示、预算控制和任务恢复。每轮生成以最近成功版本为基线创建独立新版本，原版本保持不变。

新增产品级保留校验：系统从基线 HTML 中提取按钮、链接、标题和表单值等可见控件文案，在新 HTML 中进行确定性检查。发现缺失时会携带缺失清单发起一次定向修复；修复未改善或预算不足时保留原始产物，并将缺失项写入版本摘要，不覆盖旧版本。

阶段三单次超时仍为 200 秒；为支持首轮生成、续写或重跑以及一次保留修复，生成总预算调整为 640 秒。任务 stale 回收阈值调整为 900 秒，前端轮询上限调整为 18 分钟。后端测试 161 项通过，前端 lint 与构建通过；真实模型的歌曲推荐双轮增量验收仍需单独执行。

# Project Module Description

- **异步智能生成流水线**
  - 通过 AI Hub 执行需求分析、结构设计和完整 HTML 生成。
  - 生成请求预创建版本和步骤后异步执行，前端轮询步骤状态。
  - 支持代码截断续写、不完整结果重跑、保留校验定向修复、预算控制、取消、恢复和 stale 清理。
  - 阶段三最多包含首轮生成及续写、重跑或一次保留修复，受 640 秒总预算约束。

- **产物级控件保留校验**
  - 从上一成功版本 HTML 中提取可见按钮、链接、标题和表单值。
  - 检查控件文案是否仍存在于新版本完整 HTML 中，不依赖标签或嵌套层级完全一致。
  - 发现缺失时生成具体缺失清单并触发一次定向修复。
  - 仅当修复确实减少缺失项时才采纳修复结果。
  - 修复失败、缺失未改善或预算不足时保留原始生成结果，并记录 `summary.missing_controls`。

- **历史记忆与指代消解**
  - 成功、失败和取消轮次均进入需求历史并附带状态。
  - “重新执行”“再试一次”等表达解释为重做原始需求。
  - `previous_html` 仅使用最近成功版本，失败或取消产物不作为增量基线。
  - 对历史上下文执行预算控制和长度截断。

- **增量生成与版本隔离**
  - 以成功版本为 HTML 基线创建独立新版本。
  - 原版本内容和哈希不会被新版本改写。
  - 阶段三提示词要求保留原有功能、按钮文案和示例数据。
  - 通过产物校验和定向修复降低模型遗漏旧控件的风险。
  - 当前仍不实现 BSDiff、AST 补丁或局部 DOM 注入；模型仍输出完整 HTML。

- **项目归属与隔离**
  - 使用 `owner_key` 标识登录用户或匿名访客。
  - 私有项目仅允许所属身份访问，其他身份返回资源不存在。
  - 演示项目对所有身份可见，但写操作返回冲突。
  - 通过 OwnerContext 和统一依赖过滤项目、版本、消息及生成步骤。

- **认证与访客身份**
  - 支持登录、认证回调、登出及受保护路由。
  - 复用平台 JWT 解析和认证 SDK。
  - 未登录用户通过 HMAC 签名 Cookie 获得稳定匿名归属。
  - 登出后清理会话状态。

- **项目与版本管理**
  - 管理项目、版本、生成步骤和消息记录。
  - 支持版本切换、历史内容恢复和回滚。
  - 新版本独立保存，回滚不会修改原版本内容。
  - 演示项目明确展示只读状态，生成失败不会覆盖成功版本。

- **工作区**
  - 三栏布局展示项目列表、智能体步骤与对话、代码及应用预览。
  - 支持步骤回放、暂停、重置、停止和恢复生成。
  - 展示 `pending`、`running`、`succeeded`、`failed`、`cancelled` 等状态。
  - 生成运行期间禁用冲突操作并展示失败原因、修复状态和缺失控件摘要。
  - 前端轮询最长支持 18 分钟。

- **沙箱预览**
  - 提取完整 HTML，校验文档结构并注入 CSP 后通过 iframe 渲染。
  - iframe 使用 `allow-scripts allow-forms`，不授予 `allow-same-origin`。
  - 源码查看器和预览使用一致的最终 HTML 内容。
  - 对截断、缺少文档结构和无效模型输出提供错误提示。

- **源代码查看与复制**
  - `CodeViewer` 展示生成源码及 SHA-256。
  - 复制操作优先使用 `navigator.clipboard`。
  - 在非安全上下文或 iframe 权限受限时降级使用隐藏文本框和 `execCommand('copy')`。
  - 复制按钮和 SHA-256 按钮均显示成功或失败状态。

- **后端 API**
  - 提供认证、项目、版本、消息、生成步骤、设置、存储及 AI Hub 接口。
  - 支持步骤查询、版本恢复、生成取消、归属过滤和任务恢复。
  - 统一处理参数校验、权限隔离、演示项目只读、生成失败、取消冲突和预算耗尽。

- **数据与迁移**
  - 使用 ORM 模型管理业务实体及项目归属字段。
  - 使用 Alembic 管理数据库迁移。
  - 提供幂等演示项目回填脚本。
  - 后台生成任务使用独立数据库会话。

- **自动化测试与验收**
  - 覆盖 HTML 处理、控件文案提取、保留校验和定向修复。
  - 覆盖上下文记忆、归属隔离、版本恢复和流水线恢复。
  - 覆盖阶段超时、640 秒预算、stale 阈值、失败或取消历史、成功 HTML 基线保护和任务取消。
  - 后端 161 项测试通过，前端 lint 和 build 通过。
  - `e2e_song_increment.py` 可用于真实模型双轮增量验收，但本轮尚未重新执行。

# Directory Tree

```text
app/
├── .mgx/
│   └── config.yaml                                  # 后端工程配置
├── backend/
│   ├── main.py                                      # FastAPI 应用入口
│   ├── lambda_handler.py                            # Serverless 处理入口
│   ├── e2e_acceptance.py                            # 端到端验收脚本
│   ├── e2e_two_round.py                             # 通用两轮增量验证
│   ├── e2e_song_increment.py                        # 歌曲推荐增量验收
│   ├── probe_upstream.py                            # 上游 AI 能力探测
│   ├── probe_stage3_snake.py                        # 阶段三耗时探针
│   ├── requirements.txt                             # 后端依赖
│   ├── requirements.default                         # 默认依赖模板
│   ├── pytest.ini                                   # pytest 配置
│   ├── alembic.ini                                  # 数据库迁移配置
│   ├── alembic/                                     # Alembic 环境和迁移版本
│   ├── core/                                        # 配置、认证、数据库、枚举和遥测
│   ├── dependencies/                                # FastAPI 依赖
│   │   └── owner.py                                 # 用户及匿名访客归属解析
│   ├── middlewares/                                 # FastAPI 中间件
│   ├── models/                                      # ORM 数据模型
│   ├── schemas/                                     # API 请求与响应结构
│   ├── routers/
│   │   └── atoms.py                                 # 生成、历史、步骤、恢复和取消接口
│   ├── services/
│   │   ├── pipeline.py                              # 生成、恢复、预算、校验和修复控制
│   │   ├── prompts.py                               # 历史上下文、保留约束和修复提示词
│   │   ├── aihub.py                                 # AI Hub 调用及读取超时封装
│   │   ├── aihub_errors.py                           # AI Hub 错误分类
│   │   ├── html_extract.py                           # HTML 提取、校验、控件抽取和 CSP 注入
│   │   └── *.py                                     # 项目、版本、消息、步骤等服务
│   ├── data_models/                                 # 数据结构定义 JSON
│   ├── mock_data/                                   # 演示种子数据
│   ├── scripts/
│   │   └── backfill_demo_owner.py                   # 演示项目归属回填脚本
│   ├── skills_docs/                                 # AI、对象存储和 SDK 文档
│   ├── tests/                                       # 后端单元测试
│   └── utils/                                       # 日志等通用工具
├── frontend/
│   ├── package.json                                 # 前端依赖与脚本
│   ├── vite.config.ts                               # Vite 配置
│   ├── tailwind.config.ts                           # Tailwind 配置
│   ├── index.html                                   # Vite 前端 HTML 入口
│   ├── src/                                         # React 应用源码
│   │   ├── App.tsx                                  # 路由、认证门控与整体布局
│   │   ├── main.tsx                                 # React 启动入口
│   │   ├── pages/                                   # 工作区、认证和博客页面
│   │   ├── components/                              # 工作区、预览、对话和项目组件
│   │   ├── components/ui/                           # 通用 UI 组件
│   │   ├── contexts/                                # React 上下文
│   │   ├── hooks/                                   # React Hooks
│   │   ├── lib/                                     # 生成、认证和剪贴板相关 API 封装
│   │   ├── api/                                     # 设置接口封装
│   │   └── ...                                      # 其他前端模块
│   ├── public/                                      # 静态资源
│   └── prerender/                                   # 博客预渲染与站点地图生成
├── CLAUDE.md                                        # AI 协作与开发约定
├── docs/                                            # 设计、规格和验收文档
├── start_app_v2.sh                                  # 应用启动脚本
└── uploads/                                         # 需求、规格和任务记录
```

# File Description Inventory

| 范围 | 主要文件 | 用途 |
|---|---|---|
| 归属解析 | `backend/dependencies/owner.py` | 解析 JWT、匿名身份和 OwnerContext |
| 生成路由 | `backend/routers/atoms.py` | 接收生成请求、步骤轮询、恢复和取消任务 |
| 生成流水线 | `backend/services/pipeline.py` | 执行多阶段生成、预算、超时、保留校验和定向修复 |
| AI 调用 | `backend/services/aihub.py`、`aihub_errors.py` | 调用上游 AI 服务、设置读取超时并分类异常 |
| 生成提示词 | `backend/services/prompts.py` | 拼接历史需求、处理指代、注入保留约束和缺失清单 |
| HTML 处理 | `backend/services/html_extract.py` | 提取完整 HTML、抽取可见控件、检查缺失项并注入 CSP |
| 工作区页面 | `frontend/src/pages/Index.tsx` | 管理生成轮询、恢复、停止、预算错误和修复状态展示 |
| 源码查看器 | `frontend/src/components/CodeViewer.tsx` | 展示源码和哈希，提供兼容复制及状态反馈 |
| 归属回填 | `backend/scripts/backfill_demo_owner.py` | 将无归属历史项目标记为只读演示项目 |
| 前端认证 | `frontend/src/contexts/AuthContext.tsx`、`frontend/src/lib/auth.ts` | 管理会话、登录、登出和认证状态 |
| 前端 API | `frontend/src/lib/atoms.ts` | 封装生成受理、步骤轮询、版本恢复和取消请求 |
| 需求输入 | `frontend/src/components/PromptInput.tsx` | 输入需求并根据任务状态控制提交或停止 |
| 版本管理 | `frontend/src/components/VersionSwitcher.tsx` | 切换、恢复版本并展示只读状态 |
| 数据访问 | `backend/models/*.py`、`backend/services/database.py` | 定义实体、归属字段和数据库操作 |
| 流水线测试 | `backend/tests/test_pipeline_recovery.py` | 验证恢复、阶段超时和任务生命周期 |
| 时间预算测试 | `backend/tests/test_time_budget_invariants.py` | 验证生成预算、修复次数和阈值不变量 |
| 上下文测试 | `backend/tests/test_context_memory.py` | 验证历史上下文和保留约束 |
| 双轮增量测试 | `backend/tests/test_two_round_increment.py` | 验证首轮隔离、二轮注入及版本行为 |
| 真实增量验收 | `backend/e2e_song_increment.py` | 验证歌曲推荐场景的版本内容、哈希和新增功能 |
| 端到端验证 | `backend/e2e_acceptance.py`、`backend/e2e_two_round.py` | 执行完整链路及双轮增量验收 |
| 测试基础设施 | `backend/tests/conftest.py`、`backend/tests/fakes.py` | 提供 SQLite、认证、会话和 FakeAIHub 支持 |
| 项目文档 | `docs/`、`uploads/specs/` | 记录设计、契约、验收证据和实施任务 |

# Technology Stack

- **后端**：Python、FastAPI、SQLAlchemy、Alembic、Pydantic、asyncio 后台任务
- **AI 能力**：AI Hub、DeepSeek、多阶段需求分析与完整 HTML 生成
- **前端**：React、TypeScript、Vite
- **样式与组件**：Tailwind CSS、shadcn 风格 UI、PostCSS
- **应用预览**：受 CSP 约束的 sandbox iframe
- **数据存储**：关系型数据库、ORM、Alembic、对象存储
- **认证安全**：JWT、HMAC 匿名 Cookie、认证 SDK、受保护路由
- **任务控制**：数据库任务状态、后台任务、预算、取消、心跳、恢复和 stale 检测
- **生成可靠性**：完整 HTML 校验、控件保留校验、定向修复、截断续写、整篇重跑和错误分类
- **上下文管理**：历史需求状态标注、指代消解、版本摘要、增量上下文和缺失项反馈
- **版本一致性**：版本更新时间、回滚恢复、SHA-256 内容校验、源码预览一致性
- **剪贴板兼容**：异步 Clipboard API、传统 `execCommand('copy')` 降级方案
- **测试与质量**：pytest、SQLite 测试库、FakeAIHub、端到端验收、ESLint、TypeScript 构建检查
- **部署入口**：Uvicorn 开发运行、Serverless Lambda 入口

# Usage

## 后端安装与运行

```bash
cd app/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
uvicorn main:app --reload
```

Windows 激活虚拟环境：

```bash
.venv\Scripts\activate
```

根据运行环境配置数据库、JWT、AI Hub、对象存储等必要环境变量。测试环境需提供 JWT 相关配置；首次运行时可按项目需要加载 `mock_data` 中的演示数据。

## 演示项目归属回填

```bash
cd app/backend
python scripts/backfill_demo_owner.py
```

## 阶段三真实负载探测

```bash
cd app/backend
python probe_stage3_snake.py
```

运行前需准备真实 AI Hub 配置。该探针用于测量阶段三完整 HTML 负载耗时，不参与正式生成任务。

## 前端安装与运行

```bash
cd app/frontend
npm install
npm run dev
```

## 前端检查与构建

```bash
npm run lint
npx tsc --noEmit
npm run build
```

## 后端测试

```bash
cd app/backend
pytest
```

## 通用端到端验证

```bash
cd app/backend
python e2e_acceptance.py
python e2e_two_round.py
```

## 歌曲推荐增量验收

```bash
cd app/backend
python e2e_song_increment.py
```

该脚本需要真实数据库、认证和 AI Hub 环境变量，会执行首轮生成及第二轮增量生成，并检查新版本、版本哈希、原有控件保留和目标功能。

## 一键启动

```bash
cd app
bash start_app_v2.sh
```
