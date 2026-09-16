# 契约：REST 接口

**Feature**: `001-atoms-demo` | **Base**: `/api`

除生成接口外，所有请求与响应均为 `application/json; charset=utf-8`。生成接口使用 SSE，见 [streaming-events.md](./streaming-events.md)。

---

## 通用约定

### ID 暴露策略

对外一律使用 `public_id`（UUID v4），**自增 `id` 不得出现在任何响应中**。这是防止项目被枚举遍历的硬性要求（对应 `data-model.md` 中 `public_id` 的设计意图）。

### 错误信封

所有非 2xx 响应统一为：

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "该项目已有正在进行的生成，请等待完成后再试"
  }
}
```

| `code` | HTTP | 含义 |
|---|---|---|
| `VALIDATION_ERROR` | 400 | 入参不合法（空描述、超长等） |
| `NOT_FOUND` | 404 | 项目或版本不存在 |
| `CONFLICT` | 409 | 并发冲突（已有进行中的生成） |
| `UPSTREAM_ERROR` | 502 | 上游模型服务失败 |
| `INTERNAL_ERROR` | 500 | 其他内部错误 |

**`message` 必须是面向用户的可读中文措辞，不得包含异常堆栈或内部路径**——它会被前端直接展示。

---

## `GET /api/health`

健康检查，同时用于验证配置与上游连通性。

**响应 200**

```json
{
  "status": "ok",
  "db": "ok",
  "llm": {
    "configured": true,
    "model": "deepseek-chat",
    "reachable": true
  }
}
```

**说明**：`llm.reachable` 由一次轻量探测请求得出（对应 research.md R4 的缓解措施）。**探测结果须缓存**（建议 60 秒），避免健康检查本身消耗配额。`llm.configured=false` 时服务仍应启动，便于本地无 key 调试。

---

## `GET /api/projects`

项目列表，按 `updated_at` 倒序。

**响应 200**

```json
{
  "projects": [
    {
      "public_id": "9f1c…",
      "title": "记账小工具",
      "created_at": "2026-09-15T10:00:00Z",
      "updated_at": "2026-09-15T10:03:12Z",
      "version_count": 2,
      "latest_status": "succeeded"
    }
  ]
}
```

**说明**：**不含 `html`**。列表接口返回完整生成结果会造成响应体无谓膨胀。`latest_status` 供前端展示生成状态。

---

## `POST /api/projects`

创建空项目。

**请求体**

```json
{ "title": "记账小工具" }
```

`title` 可选，省略时由后端生成占位名（如「未命名项目」），并在首次生成完成后由阶段 1 的产出覆盖。

**响应 201**

```json
{
  "public_id": "9f1c…",
  "title": "未命名项目",
  "created_at": "2026-09-15T10:00:00Z",
  "updated_at": "2026-09-15T10:00:00Z",
  "version_count": 0,
  "latest_status": null
}
```

---

## `GET /api/projects/{public_id}`

项目详情，含**版本列表**与**对话记录**，但**不含 `html`**。

**响应 200**

```json
{
  "public_id": "9f1c…",
  "title": "记账小工具",
  "created_at": "2026-09-15T10:00:00Z",
  "updated_at": "2026-09-15T10:03:12Z",
  "versions": [
    {
      "seq": 1,
      "prompt": "做一个记账小工具…",
      "status": "succeeded",
      "error": null,
      "duration_ms": 41230,
      "created_at": "2026-09-15T10:03:12Z",
      "steps": [
        { "seq": 1, "name": "需求分析", "status": "succeeded" },
        { "seq": 2, "name": "结构设计", "status": "succeeded" },
        { "seq": 3, "name": "代码生成", "status": "succeeded" }
      ]
    }
  ],
  "messages": [
    { "role": "user", "content": "做一个记账小工具…", "version_seq": null, "created_at": "…" },
    { "role": "assistant", "content": "已生成「记账小工具」", "version_seq": 1, "created_at": "…" }
  ]
}
```

**错误**：`404 NOT_FOUND`

**说明**：HTML 体积大，列表与详情接口一律不返回，由版本详情接口按需获取。

---

## `GET /api/projects/{public_id}/versions/{seq}`

单个版本的完整内容，**含 `html`**。

**响应 200**

```json
{
  "seq": 1,
  "prompt": "做一个记账小工具…",
  "html": "<!DOCTYPE html>…",
  "summary": { "features": ["记账", "余额展示"], "structure": "单页 + localStorage" },
  "status": "succeeded",
  "duration_ms": 41230,
  "created_at": "2026-09-15T10:03:12Z"
}
```

**错误**：`404 NOT_FOUND`

**⚠️ 安全约束**：`html` 是**不可信输入**（由大模型生成）。消费方**必须**在无 `allow-same-origin` 的沙箱中渲染，参见 [streaming-events.md](./streaming-events.md) 中的渲染约束。

---

## `DELETE /api/projects/{public_id}`

删除项目及其下全部版本、消息与步骤（级联）。

**响应 204**，无响应体。

**错误**：`404 NOT_FOUND`

---

## `POST /api/projects/{public_id}/generate`

**使用 SSE 流式响应**，契约见 [streaming-events.md](./streaming-events.md)。

**请求体**

```json
{ "prompt": "再加一个按月份筛选的图表" }
```

| 字段 | 约束 |
|---|---|
| `prompt` | 必填，去除首尾空白后长度 1–2000 字符 |

**错误**（在流开始前以普通 JSON 返回）：

| 场景 | code | HTTP |
|---|---|---|
| `prompt` 为空或超长 | `VALIDATION_ERROR` | 400 |
| 项目不存在 | `NOT_FOUND` | 404 |
| 该项目已有进行中的生成 | `CONFLICT` | 409 |
| 未配置 API Key | `INTERNAL_ERROR` | 500 |

**⚠️ 关键**：`CONFLICT` 与 `VALIDATION_ERROR` **必须在 SSE 流开始前返回**。一旦响应头以 `text/event-stream` 发出，就无法再改 HTTP 状态码，错误只能作为事件推送——这会让前端的错误处理复杂化。
