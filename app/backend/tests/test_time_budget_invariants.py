"""时间预算不变量测试（FR-001/FR-013，contracts/generation-lifecycle.md）。

本文件守护本特性最核心的一条不等式链：

    STAGE_TIMEOUT(120s) < GENERATION_BUDGET_SECONDS(420s)
                        < STALE_AFTER(600s) < 前端轮询上限(720s)

它必须由**测试**守护而不是由注释守护，因为这条链一旦被破坏，症状是线上
「UI 永远转圈」这种无异常、无日志的静默故障，代码评审很难看出来。

前端上限**从 Index.tsx 读取真实值**，不在本文件重新硬编码——否则测试只是
把自己写的常数和自己写的常数比较，等于自证（spec 第 8 条点名的缺口）。
"""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

from core.config import settings
from routers import atoms as atoms_module
from services import pipeline as pipeline_module

FRONTEND_INDEX = (
    Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "Index.tsx"
)
_FRONTEND_CONST = re.compile(
    r"const\s+GENERATION_POLL_TIMEOUT_MS\s*=\s*([0-9\s*+]+);"
)
# 只允许数字与 * + 的算术表达式，避免为了求值引入 eval。
_SAFE_ARITHMETIC = re.compile(r"^[0-9*+\s]+$")


def _frontend_poll_timeout_seconds() -> float:
    """从 Index.tsx 解析 GENERATION_POLL_TIMEOUT_MS，换算为秒。

    前端文件缺失或常量被改名时**直接失败**——静默跳过会让这条不变量在
    前端重构后无人守护。
    """
    source = FRONTEND_INDEX.read_text(encoding="utf-8")
    match = _FRONTEND_CONST.search(source)
    assert match, (
        f"未能从 {FRONTEND_INDEX} 解析 GENERATION_POLL_TIMEOUT_MS；"
        "若常量被改名，请同步更新本测试（不要改成硬编码）"
    )
    expression = match.group(1).strip()
    assert _SAFE_ARITHMETIC.match(expression), (
        f"GENERATION_POLL_TIMEOUT_MS 表达式含非算术字符，拒绝求值: {expression!r}"
    )
    milliseconds = 1
    for factor in expression.split("*"):
        milliseconds *= int(factor.strip())
    return milliseconds / 1000.0


def test_budget_outlives_a_single_stage_call():
    """120 < 420：单次调用必须能在总预算内至少跑完一次并留有恢复余地。

    否则预算被单次调用吃光，任何续写/重跑恢复都不可能发生。
    """
    assert pipeline_module.STAGE_TIMEOUT < pipeline_module.GENERATION_BUDGET_SECONDS


def test_one_stage_worst_case_fits_inside_budget():
    """单阶段的**全部重试**最坏耗时必须小于总预算。

    这是修复前的核心漏洞：每次调用各自 240s，重试乘积无人约束，
    6 次 _call_step 最坏 4329s ≈ 72 分钟，而前端只等 12 分钟。
    """
    worst_per_stage = (
        pipeline_module.MAX_ATTEMPTS * pipeline_module.STAGE_TIMEOUT
        + sum(pipeline_module.RETRY_BACKOFF_SECONDS)
    )
    assert worst_per_stage < pipeline_module.GENERATION_BUDGET_SECONDS, (
        f"单阶段最坏 {worst_per_stage}s 已超出总预算 "
        f"{pipeline_module.GENERATION_BUDGET_SECONDS}s"
    )


def test_budget_shorter_than_stale_recovery():
    """420 < 600：活任务的最长静默期必须短于回收阈值。

    活任务在 versions.updated_at 上的静默期不超过总预算（详见 pipeline.py
    末尾注释：_call_step 只改 generation_steps，不推进 versions.updated_at）。
    若预算反超回收阈值，活任务会被 _recover_stale_versions 误杀——
    这正是心跳当初要解决的问题。
    """
    budget = pipeline_module.GENERATION_BUDGET_SECONDS
    stale_after = atoms_module.STALE_AFTER.total_seconds()
    assert atoms_module.STALE_AFTER == timedelta(minutes=10)
    assert budget < stale_after, (
        f"预算 {budget}s 不得大于等于回收阈值 {stale_after}s，否则活任务会被误杀"
    )


def test_stale_recovery_shorter_than_frontend_patience():
    """600 < 720：回收必须先于前端放弃，否则用户永远等不到真实终态。

    这是当前生产「UI 永久生成中」的直接成因。
    """
    stale_after = atoms_module.STALE_AFTER.total_seconds()
    frontend = _frontend_poll_timeout_seconds()
    assert stale_after < frontend, (
        f"回收阈值 {stale_after}s 必须小于前端上限 {frontend}s"
    )


def test_min_call_budget_below_stage_timeout():
    """最小可用调用预算必须真的「够跑一次」，否则闸门会提前误判为耗尽。"""
    assert (
        pipeline_module.MIN_CALL_BUDGET_SECONDS
        < pipeline_module.STAGE_TIMEOUT
    )


def test_heartbeat_is_gone():
    """心跳不得以任何形式复活（tasks.md 陷阱 2）。

    心跳刷新的正是回收判据所用的 versions.updated_at，因此它和回收机制
    在逻辑上互斥：有心跳就永远回收不了。该职责已由总预算接管。
    """
    assert not hasattr(pipeline_module, "HEARTBEAT_INTERVAL"), (
        "HEARTBEAT_INTERVAL 不应再存在——心跳与 stale 回收在逻辑上互斥，"
        "见 services/pipeline.py 末尾注释"
    )
    assert not hasattr(
        pipeline_module.GenerationPipeline, "_heartbeat"
    ), "GenerationPipeline._heartbeat 不应再存在"


def test_upstream_client_declares_its_own_wait_limit(monkeypatch):
    """FR-012 / R-9：上游客户端的等待上限必须**显式声明**，不得依赖库默认值。

    实测 openai 2.x 的默认值是 ``Timeout(connect=5, read=600, write=600,
    pool=600)`` + ``max_retries=2``。read 600s 远大于 ``STAGE_TIMEOUT``，于是
    自有 ``wait_for`` 先超时放弃、库仍在后台等满 600s——每次尝试都留下一条
    悬挂连接；而 2 次库级重试又叠在自有重试之上，使实际等待时间不可预测。
    两者都不是「等待上限被显式声明」的形态。
    """
    from services.aihub import AIHubService

    monkeypatch.setenv("APP_AI_BASE_URL", "https://ai.invalid")
    monkeypatch.setenv("APP_AI_KEY", "test-key")
    # 逼 settings.__getattr__ 重新读环境变量（缓存回滚由 conftest 兜底）
    settings.__dict__.pop("app_ai_base_url", None)
    settings.__dict__.pop("app_ai_key", None)

    service = AIHubService()
    assert service.client is not None, (
        "客户端未构造——环境变量未生效，是测试自身的问题"
    )
    assert service.client.timeout == pipeline_module.STAGE_TIMEOUT, (
        f"上游客户端等待上限为 {service.client.timeout!r}，未与 STAGE_TIMEOUT "
        f"({pipeline_module.STAGE_TIMEOUT}s) 对齐"
    )
    assert service.client.max_retries == 1, (
        f"库级重试次数为 {service.client.max_retries!r}，会与自有重试叠加放大等待"
    )
