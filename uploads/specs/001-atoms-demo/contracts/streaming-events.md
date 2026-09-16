# 契约：生成过程的流式事件

**Feature**: `001-atoms-demo` | **接口**: `POST /api/projects/{public_id}/generate`

响应头：

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache
X-Accel-Buffering: no
```

（`X-Accel-Buffering: no` 用于禁用 Nginx 的响应缓冲，**缺少它会导致流被攒到最后一次性吐出，流式完全失效**。）

---

## 帧格式

标准 SSE：`event:` 行 + `data:` 行 + **空行**结束。`data` 为单行 JSON。

```
event: step_started
data: {"seq":1,"name":"需求分析"}

```

**心跳**：每 15 秒发送一次注释行 `: keep-alive`，防止中间代理因空闲超时断开连接。客户端解析时**必须忽略以 `:` 开头的行**。

---

## 事件序列

一次成功的生成按以下顺序发射：

```
run_started → (step_started → step_delta* → step_completed)* ×3 → run_completed
```

任一阶段失败则：

```
… → step_failed → run_failed
```

### `run_started`

流开始，携带预置的三个步骤。**此事件必须在建立连接后立即发送**。

```json
{ "version_seq": 2, "steps": [
  { "seq": 1, "name": "需求分析" },
  { "seq": 2, "name": "结构设计" },
  { "seq": 3, "name": "代码生成" }
] }
```

### `step_started`

某一阶段开始。

```json
{ "seq": 1, "name": "需求分析" }
```

### `step_delta`

该阶段的增量输出片段，用于展示「智能体正在思考」。**可选事件**——前端应能在完全不收到 `step_delta` 的情况下正常工作。

```json
{ "seq": 1, "text": "用户需要一个记账工具，" }
```

### `step_completed`

某一阶段成功结束。`summary` 为该阶段的简短产出摘要（用于展示，非完整内容）。

```json
{ "seq": 1, "status": "succeeded", "summary": "识别出 3 项核心功能" }
```

### `step_failed`

某一阶段失败。**发送后必须紧跟 `run_failed`**。

```json
{ "seq": 3, "status": "failed", "message": "生成内容无法解析为有效页面" }
```

### `run_completed`

生成成功。`html` 为最终结果。

```json
{ "version_seq": 2, "duration_ms": 41230, "html": "<!DOCTYPE html>…" }
```

### `run_failed`

生成失败。`message` 为**面向用户的可读中文措辞**。

```json
{ "version_seq": 2, "message": "模型服务暂时不可用，请稍后重试" }
```

---

## 客户端实现约束

### ⚠️ 必须使用 `fetch` + `ReadableStream`，不能用 `EventSource`

浏览器原生 `EventSource` **只支持 GET 且无法携带请求体**，而本接口需要 POST 提交 `prompt`。因此前端须：

1. `fetch(url, { method: 'POST', body: JSON.stringify({prompt}) })`
2. 读取 `response.body.getReader()`
3. 按 `\n\n` 切分帧，逐行解析 `event:` 与 `data:`
4. **忽略 `:` 开头的注释行**（心跳）
5. 处理**跨 chunk 截断**——一个完整的帧可能被 TCP 分片切成多次 `read()` 返回，必须维护缓冲区而非对每个 chunk 独立解析

第 5 点是本项目最容易写错、也最难排查的地方（表现为「偶发丢事件」）。建议将该解析逻辑独立为 `api/stream.ts` 中的纯函数并配单元测试。

### 中断与恢复

- 用户主动取消：调用 `AbortController.abort()`，服务端检测到连接断开后应停止后续阶段调用，避免无谓的 token 消耗
- 连接意外中断：客户端应展示可重试的提示；服务端侧原本 `running` 的记录在下次启动清理时置为 `failed`

---

## ⚠️ 渲染约束（安全关键）

`run_completed.html` 与 `GET /versions/{seq}` 的 `html` 字段**均为不可信输入**——它们由大模型生成，且直接来自用户提示词的影响范围。

渲染时**必须**满足：

```html
<iframe sandbox="allow-scripts allow-forms" srcdoc="..."></iframe>
```

| 约束 | 原因 |
|---|---|
| **禁止**添加 `allow-same-origin` | 与 `allow-scripts` 同时存在时，iframe 将获得与父页面相同的源，可读取父页面的 DOM、`localStorage` 与 cookie——等于完全绕过隔离 |
| 注入 CSP 到 `srcdoc` | `default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data: blob:` —— 阻断外联，防止数据外泄，同时保证结果自包含 |
| **禁止**提供动态覆盖 sandbox 属性的入口 | 该属性须以常量固定在 `PreviewPane.vue` 中，不接受 props 或配置注入 |

**已知限制**：不透明源（opaque origin）下生成的应用**无法使用 `localStorage`**。这是有意的取舍，与 spec 中 FR-016 的分期决策一致——应用级持久化属于延展项，届时需通过 `postMessage` 通道由宿主代为存储。
