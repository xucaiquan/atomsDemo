# 002-harden-increment-session 证据留档

本目录存放特性 `002-harden-increment-session`（增量可靠性与会话连续性加固）的验收证据。

## 命名约定

`<日期>-<被测版本>-<场景>.txt`

- `<日期>`：采集日期，格式 `YYYYMMDD`
- `<被测版本>`：被测代码状态标识（如 `baseline` 改动前基线、`final` 最终全绿）
- `<场景>`：场景名（见下）

## 必留场景（三个）

1. **本地回归**：`cd app/backend && python -m pytest tests/ -q` 的完整输出（含用例名与统计行）。
2. **线上探针**：`app/backend/probe_upstream.py` / `app/backend/e2e_two_round.py` 的运行输出
   （消耗真实模型配额；配额不足时保留失败输出并注明原因）。
3. **两轮端到端**：两轮增量生成 + 旧功能保留的产物级核对输出。

## 与任务文档的谱系差异说明

任务文档假设仓库存在 `tests/test_generation_inline.py`（3 红）、`tests/test_fake_aihub.py`（1 红）、
`tests/test_conftest_smoke.py`（2 error）及 `bd78a46` 合并产物；当前仓库经核查**不存在**这些文件
（`app/backend/tests/` 仅含 7 个既有测试文件），因此 T008 的「修复 6 个身份无关红灯」在本仓库
无对应对象，T039 所列探针中间 JSON 亦不存在。基线以本仓库真实状态录制（见 baseline-tests.txt）。
