# Quickstart 验证指南: 智能体驱动的应用生成平台

**Feature**: `001-atoms-demo` | **Date**: 2026-09-15

本文件是**验证与运行指南**，用于证明功能端到端可用。实现细节见 [plan.md](./plan.md)，数据与接口定义见 [data-model.md](./data-model.md) 与 [contracts/](./contracts/)。

---

## 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | 3.12+ | 后端 |
| Node.js | 20+ | 前端构建 |
| MySQL | 8.0+ | 字符集须为 `utf8mb4` |
| DeepSeek API Key | — | 写入 `backend/app/config.py`（由 `config.example.py` 复制） |

---

## ✅ 第 0 步：DeepSeek 连通性与模型选型 —— 已于 2026-09-15 验证通过

**结论：使用 `deepseek-chat`。本步骤已完成，无需重复。** 完整实测记录见 [research.md](./research.md) R4。

| 模型 ID | 状态 | 说明 |
|---|---|---|
| **`deepseek-chat`** | ✅ **用这个** | 遗留别名，静默指向 `deepseek-flash` 的**非思考**模式。6.8–13 s 产出完整 HTML |
| `deepseek-flash` | ❌ **不要用** | 思考模型，`max_tokens` 被 `reasoning_content` 吃光，**`content` 返回空串** |
| `deepseek-v4-pro` | 🔸 备用 | 质量更高，但耗时约 24 s。代码生成质量不足时可切换 |

**⚠️ 两个必须记住的坑**：

1. `GET /models` 只返回 `deepseek-flash` 与 `deepseek-v4-pro`，**不含 `deepseek-chat`**——但 `deepseek-chat` 可正常调用。**切勿用模型列表判断可用性。**
2. `deepseek-flash` **直连会返回 HTTP 200 但内容为空**。启动探测**必须校验 `content` 非空**，而非仅看状态码。

**⚠️ 部署后必须重做的一件事**：在**服务器所在网络**（腾讯云香港）重新实测连通性与延迟。上述验证在本地网络完成，**两地网络质量未必一致**：

```bash
curl -o /dev/null -s -w "connect=%{time_connect}s total=%{time_total}s\n" \
  https://api.deepseek.com/chat/completions
```

---

## 本地启动

**后端**

```bash
cd backend
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash
pip install -r requirements.txt
# 复制 config.example.py 为 config.py 并填入 API Key 与数据库连接
uvicorn app.main:app --reload --port 8000
```

**前端**

```bash
cd frontend
npm install
npm run dev     # 默认 http://localhost:5173，代理 /api 到 :8000
```

**数据库**

创建库 `atoms_demo`（`CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci`），表结构由 SQLAlchemy 建表逻辑生成。

---

## 端到端验证场景

每个场景对应 spec 中的用户故事与成功标准。**按序执行**——后面的场景依赖前面的产出。

### V1 — 生成闭环（对应 US1 / SC-001、SC-004、SC-006）

1. 打开首页，输入框为空
2. 输入：`做一个记账小工具，能记收入和支出，显示总余额`
3. 提交

**期望**：
- ✅ **3 秒内**界面出现三个步骤项，第一项为「进行中」——**此反馈完全由客户端渲染，不等待网络**
- ✅ 各步骤状态随生成推进依次变化（US4 / SC-008）
- ✅ **90 秒内**右侧渲染出可交互的应用
- ✅ 在应用内**输入一笔金额并点击提交，余额数字真实变化**（非静态占位）
- ✅ 全过程界面无超过 5 秒的无响应状态（SC-002）

### V2 — 持久化（对应 US2 / SC-003）**—— 必验项**

1. 完成 V1 后，**刷新页面**
2. 在项目列表中找到刚才的项目并打开
3. **重启后端服务**，再次刷新

**期望**：
- ✅ 刷新后项目、生成结果、对话记录均完整可见
- ✅ 服务重启后数据**不丢失**（此条为 FR-004 的核心，也是原始需求「数据持久化」的验收点）

### V3 — 迭代修改（对应 US3 / FR-006）

1. 在已有项目下追加：`再加一个按月份筛选的图表`
2. 等待生成完成

**期望**：
- ✅ 产出**新版本**（`seq` 递增）
- ✅ 新版本**保留原有记账功能**，而非推倒重来
- ✅ 版本列表中可切换回 `seq=1` 并正确渲染

### V4 — 沙箱隔离（对应 FR-010 / SC-005）**—— 安全必验项**

在浏览器的开发者工具中，选中渲染生成结果的 iframe，确认：

**期望**：
- ✅ `sandbox` 属性为 `allow-scripts allow-forms`，**不含** `allow-same-origin`
- ✅ 在生成的应用内执行 `window.parent.document` 时**抛出跨域错误**（证明隔离生效）
- ✅ 在生成的应用内访问 `localStorage` 时**抛出不透明源错误**（此为已知的预期限制，见 contracts/streaming-events.md）

### V5 — 失败路径（对应 FR-011 / SC-007）

1. 临时将 `config.py` 中的 API Key 改为无效值
2. 提交一次生成

**期望**：
- ✅ 界面在对应步骤显示失败状态，并给出**可读的中文提示**（不含堆栈）
- ✅ **用户输入的描述未丢失**——它已落库，刷新后仍可见（FR-011）
- ✅ 数据库中该版本 `status='failed'` 且 `error` 非空

### V6 — 并发约束（对应 FR-012）

1. 在一次生成**进行中**时，对同一项目再次提交

**期望**：
- ✅ 第二次请求被拒绝，返回 `409 CONFLICT`
- ✅ 提示文案面向用户可读

---

## 部署验证

```bash
cd deploy
# 复制 .env.example 为 .env 并填入密钥与数据库配置
docker compose up -d
docker compose ps          # 三个服务均应为 healthy
```

**核验项**：

- ✅ `https://<域名>/` 返回前端页面
- ✅ `https://<域名>/api/health` 返回 `{"status":"ok","db":"ok","llm":{"reachable":true}}`
- ✅ **流式生效**——观察浏览器 Network 面板，生成请求应为多条渐进到达的事件，**而非最后一次性返回**（若被缓冲，检查 `X-Accel-Buffering: no` 与 Nginx 的 `proxy_buffering off`）
- ✅ **MySQL 未对公网开放**——从外部尝试连接 `3306` 应被拒绝
- ✅ 从一台**非服务器所在网络**的机器访问链接，确认可正常打开（模拟评估者视角）

---

## 交付前核验清单

对应原始需求的交付要求与 plan 的 G2 门禁：

- [ ] **`.gitignore` 生效**——`git status` 中**不出现** `backend/app/config.py` 与 `deploy/.env`
- [ ] **仓库中无密钥**——对全仓执行密钥扫描（如 `grep -rE "sk-[A-Za-z0-9]{20,}" .`）确认无命中
- [ ] `config.example.py` 已提交且 Key 字段为空
- [ ] 仓库权限为 **public**
- [ ] Demo 访问链接可**匿名打开**（用无痕窗口验证）
- [ ] 数据库中**已有演示数据**——评估者打开时项目列表不为空
- [ ] 说明文档已包含：实现思路、关键取舍、完成程度、后续扩展优先级
- [ ] 已截图 AI 工具使用账单（原始需求中的加分项）
