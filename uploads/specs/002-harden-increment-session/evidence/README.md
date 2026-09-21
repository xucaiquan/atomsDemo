# 验收证据留档

本目录存放 `002-harden-increment-session` 的可复跑验证证据。

## 命名约定

```
<日期>-<被测版本>-<场景>.txt|.md
```

- `<日期>`：`YYYY-MM-DD`
- `<被测版本>`：git 短哈希，或 `local` 表示未提交的工作区
- `<场景>`：见下表

## 三个必留场景

| 场景 | 来源 | 成本 | 断言口径 |
|---|---|---|---|
| 本地回归 | `cd app/backend && .venv/Scripts/python -m pytest tests/ -q` | 零 | 0 failed / 0 errors（SC-011） |
| 线上探针 | `app/backend/probe_upstream.py` | **消耗配额** | 区分「上游拒绝了某阶段载荷」与「流水线自身问题」 |
| 两轮端到端 | `app/backend/e2e_two_round.py` | **消耗配额** | 两轮均达终态、产物级旧功能保留、会话恢复（SC-001/002/007/008/012） |

## 重要：运行环境

后端测试**必须用 venv 里的 Python**。系统 Python（`AppData/Local/Programs/Python/Python312`）没有装 pytest：

```bash
cd app/backend
.venv/Scripts/python.exe -m pytest tests/ -q
```

必须用 `python -m pytest`（仓库根无 conftest/pyproject，靠 `-m` 把 CWD 放进 `sys.path`；直接 `pytest` 会 ImportError）。

## 副作用告知

线上场景（探针 / 端到端）会创建真实项目并消耗模型配额。运行前请确认成本可接受，并知会相关方。
