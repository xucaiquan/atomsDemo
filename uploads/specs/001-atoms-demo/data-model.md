# Phase 1 数据模型: 智能体驱动的应用生成平台

**Feature**: `001-atoms-demo` | **Date**: 2026-09-15

对应 [spec.md](./spec.md) 中的四个关键实体。所有表使用 `utf8mb4` / `utf8mb4_0900_ai_ci` / InnoDB。

---

## 实体关系

```
projects ──┬──< versions ──┬──< generation_steps
           │               │
           └──< messages ──┘ (version_id 可空)
```

- 一个**项目**拥有多个**生成版本**与多条**对话消息**
- 一个**生成版本**拥有多个**生成步骤**（固定 3 条）
- 一条**对话消息**可关联到触发它的生成版本（用户消息关联为空）

---

## projects — 项目

用户围绕一个应用想法所展开的全部工作的载体。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK, AUTO_INCREMENT | 内部主键，不对外暴露 |
| `public_id` | CHAR(36) | UNIQUE, NOT NULL | 对外标识（UUID v4）。**避免自增 ID 被枚举遍历他人项目** |
| `title` | VARCHAR(120) | NOT NULL | 项目名。取自需求分析阶段产出的应用名；缺失时回退为用户描述的前 20 字 |
| `created_at` | DATETIME(3) | NOT NULL | |
| `updated_at` | DATETIME(3) | NOT NULL | 每次新增版本时更新，用于列表排序 |

**索引**：`UNIQUE(public_id)`、`INDEX(updated_at)`

**验证规则**：
- `title` 为空时不允许创建（回退逻辑须保证非空）
- 项目删除时级联删除其下所有版本、消息与步骤

---

## versions — 生成版本

一次完整生成所产出的结果快照。多个版本构成项目的演进历史。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK, AUTO_INCREMENT | |
| `project_id` | BIGINT UNSIGNED | FK → `projects.id`, ON DELETE CASCADE, NOT NULL | |
| `seq` | INT UNSIGNED | NOT NULL | 项目内序号，从 1 开始递增 |
| `prompt` | TEXT | NOT NULL | 本次生成的用户输入。**在生成开始前即写入**，满足 FR-011 |
| `html` | MEDIUMTEXT | NULL | 生成结果。失败时为 NULL |
| `summary` | JSON | NULL | 阶段 1、2 的产出摘要（需求要点、结构说明），用于回放与调试 |
| `status` | VARCHAR(16) | NOT NULL | `pending` / `running` / `succeeded` / `failed` |
| `error` | TEXT | NULL | 失败原因，**存储面向用户的措辞**而非异常堆栈 |
| `duration_ms` | INT UNSIGNED | NULL | 端到端耗时，用于验证 SC-001 的 90 秒指标 |
| `created_at` | DATETIME(3) | NOT NULL | |

**索引**：`UNIQUE(project_id, seq)`、`INDEX(project_id, created_at)`

**状态流转**：

```
                    ┌──────────────► succeeded   (生成成功，html 非空)
                    │
pending ──► running ┼──────────────► failed      (生成失败/超时，error 非空)
   │                │
   └────────────────┴──────────────► failed      (提交即被拒，如并发冲突)
```

**验证规则**：
- `status='succeeded'` 时 `html` 必须非空
- `status='failed'` 时 `error` 必须非空
- `status='succeeded'` 时 `html` 必须先通过 `html_extract` 的三级容错提取
- **并发约束（FR-012）**：同一 `project_id` 下已存在 `status IN ('pending','running')` 的记录时，拒绝新的生成请求

**恢复语义**：服务重启后，残留的 `pending` / `running` 记录视为中断，由启动清理逻辑置为 `failed` 并写入可读原因，避免用户看到永久卡住的加载态。

---

## messages — 对话消息

用户与系统之间的交互记录，承载多轮迭代的上下文。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK, AUTO_INCREMENT | |
| `project_id` | BIGINT UNSIGNED | FK → `projects.id`, ON DELETE CASCADE, NOT NULL | |
| `role` | VARCHAR(16) | NOT NULL | `user` / `assistant` |
| `content` | TEXT | NOT NULL | 用户消息为原始描述；助手消息为结果的简要描述 |
| `version_id` | BIGINT UNSIGNED | FK → `versions.id`, ON DELETE SET NULL, NULL | 助手消息指向其对应的生成版本 |
| `created_at` | DATETIME(3) | NOT NULL | |

**索引**：`INDEX(project_id, created_at)`

**说明**：助手消息**不存储完整 HTML**（HTML 已在 `versions.html`），仅存展示用的摘要，避免重复占用空间。

---

## generation_steps — 生成步骤

一次生成过程中所经历的阶段性环节及其状态。用于支撑 spec 的 P4 用户故事与 FR-008。

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | BIGINT UNSIGNED | PK, AUTO_INCREMENT | |
| `version_id` | BIGINT UNSIGNED | FK → `versions.id`, ON DELETE CASCADE, NOT NULL | |
| `seq` | TINYINT UNSIGNED | NOT NULL | 1..3 |
| `name` | VARCHAR(60) | NOT NULL | 展示名：`需求分析` / `结构设计` / `代码生成` |
| `status` | VARCHAR(16) | NOT NULL | `pending` / `running` / `succeeded` / `failed` |
| `output` | MEDIUMTEXT | NULL | 该阶段的原始产出，用于回放智能体过程 |
| `started_at` | DATETIME(3) | NULL | |
| `ended_at` | DATETIME(3) | NULL | |

**索引**：`UNIQUE(version_id, seq)`

**说明**：
- 每个版本创建时**预置 3 条** `status='pending'` 的记录，使前端可在提交后**立即**渲染出完整的步骤列表——这是满足 SC-002「3 秒内首个可见反馈」的关键：**首个反馈完全由客户端本地渲染，不依赖任何网络往返**
- 步骤记录是 P4 可视化的持久化基础；即使不做回放，它也为失败排查提供了阶段粒度的定位能力

---

## 容量估算

| 表 | 单条大小 | 预期行数（演示期） | 说明 |
|---|---|---|---|
| `projects` | ~200 B | < 100 | |
| `versions` | 10–50 KB | < 300 | 主要占用来自 `html` |
| `messages` | ~1 KB | < 600 | |
| `generation_steps` | 1–5 KB | < 900 | |

**结论**：演示期总容量在 20 MB 量级，4GB 内存的实例完全无压力。**无需分库分表或归档策略**（符合 G4 复杂度克制门禁）。
