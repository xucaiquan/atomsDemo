"""生成链路故障恢复测试（设计文档 S3 / S4，验收 A3）。

GENERATION_INLINE=1 让受理接口在当前协程内跑完流水线：post(generate)
返回时版本已处于终态，无需轮询，测试确定且快速。
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import select

from core.database import db_manager
from fakes import (
    ANALYSIS_JSON,
    DESIGN_JSON,
    AuthenticationError,
    FakeAIHub,
    InternalServerError,
    PermissionDeniedError,
    RateLimitError,
    make_html,
)
from models.versions import Versions
from services import pipeline as pipeline_module
from services.pipeline import classify_upstream_error


async def _create_project(client) -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": "t"})
    return response.json()["public_id"]


async def _generate(client, pid: str, prompt: str):
    return await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": prompt}
    )


async def _version_detail(client, pid: str, seq: int):
    return await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}")


# ------------------------------------------------------------------ 分类器


def test_classify_status_codes():
    auth = AuthenticationError("401")
    assert classify_upstream_error(auth).kind == "auth"
    assert classify_upstream_error(auth).retriable is False
    assert classify_upstream_error(PermissionDeniedError("403")).kind == "auth"
    assert classify_upstream_error(RateLimitError("429")).kind == "rate_limit"
    assert classify_upstream_error(InternalServerError("503")).kind == "upstream_5xx"
    assert classify_upstream_error(asyncio.TimeoutError()).kind == "timeout"
    unknown = classify_upstream_error(RuntimeError("boom"))
    assert unknown.kind == "unknown" and unknown.retriable is True


# ------------------------------------------------------------------ 成功路径


async def test_happy_path_succeeds(http, inject_fake_ai):
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("番茄钟")])
        )
        accepted = await _generate(client, pid, "做一个番茄钟")
        assert accepted.status_code == 202
        seq = accepted.json()["version_seq"]

        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "succeeded"
        assert detail["html"].startswith("<!DOCTYPE html>")
        assert "Content-Security-Policy" in detail["html"]
        assert len(fake.requests) == 3

        steps = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps")
        ).json()["steps"]
        assert [s["status"] for s in steps] == ["succeeded"] * 3

        project = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
        # 阶段 1 的 app_name 覆盖占位标题
        assert project["title"] == "测试应用"
        assert project["latest_status"] == "succeeded"


# ------------------------------------------------------------------ 退避重试


async def test_rate_limit_retries_then_succeeds(http, inject_fake_ai):
    """429 用掉全部重试额度之前成功：版本终态 succeeded。

    尝试次数从 ``MAX_ATTEMPTS`` 推导（不是写死的 3）：重试额度是预算约束下的
    可调参数，写死会让「重试一次就成功」这类用例在额度调整后静默失去意义。
    """
    retries = pipeline_module.MAX_ATTEMPTS - 1
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub(
                [ANALYSIS_JSON, DESIGN_JSON]
                + [RateLimitError("429")] * retries
                + [make_html("x")]
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        assert (await _version_detail(client, pid, seq)).json()["status"] == "succeeded"
        # 阶段 1 + 阶段 2 + 失败重试 + 成功那一次
        assert len(fake.requests) == 3 + retries


async def test_rate_limit_exhausted_marks_failed(http, inject_fake_ai):
    """429 耗尽全部重试额度 → failed，且 attempts/upstream_status 可观测（S3.5）。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub(
                [ANALYSIS_JSON, DESIGN_JSON]
                + [RateLimitError("429")] * pipeline_module.MAX_ATTEMPTS
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "rate_limit"  # S3.5 可观测
        assert detail["summary"]["upstream_status"] == 429
        assert detail["summary"]["attempts"] == pipeline_module.MAX_ATTEMPTS
        assert "限流" in detail["error"]  # 面向用户的可读中文
        assert len(fake.requests) == 2 + pipeline_module.MAX_ATTEMPTS
        steps = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps")
        ).json()["steps"]
        assert steps[2]["status"] == "failed"


async def test_auth_error_fails_fast_without_retry(http, inject_fake_ai):
    """401/403（含余额不足）不重试：恰好 3 次调用后立即失败。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, PermissionDeniedError("403 insufficient balance")])
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "auth"
        assert "鉴权" in detail["error"]
        assert len(fake.requests) == 3


# ------------------------------------------------------------------ 超时


async def test_stage_timeout_classified_as_timeout(http, inject_fake_ai, monkeypatch):
    """每次调用超过 STAGE_TIMEOUT → 按 timeout 分类，重试耗尽后失败。"""
    monkeypatch.setattr(pipeline_module, "STAGE_TIMEOUT", 0.05)

    async def slow() -> str:
        await asyncio.sleep(5)
        return make_html("never")

    client = http()
    async with client:
        pid = await _create_project(client)
        inject_fake_ai(
            FakeAIHub(
                [ANALYSIS_JSON, DESIGN_JSON]
                + [lambda req: slow()] * pipeline_module.MAX_ATTEMPTS
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "timeout"
        assert "超时" in detail["error"]


# ------------------------------------------------------------------ 空内容降级


async def test_empty_content_retries_with_lower_max_tokens(http, inject_fake_ai):
    """空内容会重试，且重试时 max_tokens 降级（16384→8192）。

    空内容次数取 ``MAX_ATTEMPTS - 1``（即用满重试额度之前的全部空响应），
    这样额度调整后本用例仍然表达「尽可能多地空、最后一次成功」的原意。
    """
    empties = pipeline_module.MAX_ATTEMPTS - 1
    client = http()
    async with client:
        pid = await _create_project(client)
        # 首个空响应故意用纯空白（验证 .strip() 生效），其余用空串
        blank_responses = ["   "] + [""] * max(empties - 1, 0)
        fake = inject_fake_ai(
            FakeAIHub([ANALYSIS_JSON, DESIGN_JSON] + blank_responses + [make_html("ok")])
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        assert (await _version_detail(client, pid, seq)).json()["status"] == "succeeded"
        assert len(fake.requests) == 2 + empties + 1
        assert fake.requests[2].max_tokens == 16384  # 第一次代码调用
        assert fake.requests[-1].max_tokens == 8192  # 降级后


async def test_all_empty_content_fails_as_empty(http, inject_fake_ai):
    client = http()
    async with client:
        pid = await _create_project(client)
        inject_fake_ai(
            FakeAIHub(
                [ANALYSIS_JSON, DESIGN_JSON] + [""] * pipeline_module.MAX_ATTEMPTS
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "empty"
        assert "空内容" in detail["error"]


# ------------------------------------------------------------------ 截断续写


async def test_truncated_output_recovers_by_continuation(http, inject_fake_ai):
    """第 1 次代码调用截断，续写片段拼接后完整 → succeeded。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        head = "<!DOCTYPE html>\n<html>\n<head></head>\n<body><p>菜谱数据很长很长"
        tail = "很长</p></body>\n</html>"
        fake = inject_fake_ai(
            FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, head, tail])
        )
        seq = (await _generate(client, pid, "每日菜谱推荐")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "succeeded"
        assert "菜谱数据很长很长" in detail["html"]
        assert detail["html"].rstrip().endswith("</html>")
        assert detail["html"].count("<html") == 1
        assert len(fake.requests) == 4
        # 续写调用的 history 回传了已产出内容（S3.3 上下文预算闸门生效处）
        assistant_msgs = [m for m in fake.requests[3].messages if m.role == "assistant"]
        assert assistant_msgs and "菜谱数据" in str(assistant_msgs[-1].content)


async def test_continuation_doc_restart_adopts_new_document(http, inject_fake_ai):
    """模型续写时重开整篇文档：直接采用新产出，不产生双份结构。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        head = "<!DOCTYPE html>\n<html>\n<head></head>\n<body><p>半截"
        restart = make_html("重开版")
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, head, restart]))
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "succeeded"
        assert detail["html"].count("<html") == 1
        assert "重开版" in detail["html"]
        assert "半截" not in detail["html"]


async def test_truncation_after_all_recoveries_fails(http, inject_fake_ai):
    """续写两轮 + 整篇重跑仍截断 → failed error_type=truncated。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        truncated = [f"<!DOCTYPE html>\n<html>\n<body><p>半截{i}" for i in range(6)]
        fake = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON] + truncated))
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "truncated"
        assert "不完整" in detail["error"]
        # 2 轮主调用 + 每轮 2 次续写 = 6 次代码阶段调用
        assert len(fake.requests) == 8


# ------------------------------------------------------------------ 取消守卫


async def test_cancel_mid_stage1_stops_before_stage2(http, inject_fake_ai):
    """阶段 1 执行期间版本被置 cancelled：阶段 2 边界检查后整体取消。"""
    client = http()
    async with client:
        pid = await _create_project(client)

        async def cancel_during_step1() -> str:
            async with db_manager.session() as session:
                version = (
                    await session.execute(
                        select(Versions).where(
                            Versions.project_public_id == pid, Versions.seq == 1
                        )
                    )
                ).scalars().first()
                version.status = "cancelled"
                version.error = "生成已取消"
                await session.commit()
            return ANALYSIS_JSON

        fake = inject_fake_ai(FakeAIHub([lambda req: cancel_during_step1()]))
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "cancelled"
        # 阶段边界后不再发起新的模型调用
        assert len(fake.requests) == 1
        steps = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps")
        ).json()["steps"]
        assert [s["status"] for s in steps] == ["succeeded", "cancelled", "cancelled"]


async def test_late_success_does_not_overwrite_cancelled(http, inject_fake_ai):
    """最后阶段完成前用户已取消：结果被丢弃，不覆盖 cancelled 终态。"""

    async def cancel_during_step3() -> str:
        async with db_manager.session() as session:
            version = (
                await session.execute(
                    select(Versions).where(
                        Versions.project_public_id == pid_holder["pid"],
                        Versions.seq == 1,
                    )
                )
            ).scalars().first()
            version.status = "cancelled"
            version.error = "生成已取消"
            await session.commit()
        return make_html("迟到的成功")

    client = http()
    pid_holder: dict = {}
    async with client:
        pid_holder["pid"] = await _create_project(client)
        inject_fake_ai(
            FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, lambda req: cancel_during_step3()])
        )
        seq = (await _generate(client, pid_holder["pid"], "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid_holder["pid"], seq)).json()
        assert detail["status"] == "cancelled"
        assert not detail["html"]  # 迟到的结果没有落库


# ------------------------------------------------------------------ 内部故障（非上游）


@pytest.mark.parametrize("fail_at_stage", [1, 2])
async def test_internal_error_is_attributed_to_actual_stage(
    http, inject_fake_ai, monkeypatch, fail_at_stage
):
    """本地代码故障必须归因到**流水线实际所在阶段**，且不留 running 步骤。

    FR-014 / FR-015 / SC-005。注入点是 ``_call_step`` 中**重试 try 之外**的
    ``GenTxtRequest`` 构造处——那些语句刻意留在 try 外（research.md R-4），
    以免我们自己的故障被误分类成「可重试的上游错误」；代价是它们由
    ``_run_stages`` 的通用兜底接管，因此兜底的归因必须准确。

    参数取 1 与 2 是关键：阶段 3 是旧实现的硬编码默认值，只有在**非 3** 的阶段
    出错才能区分「真的归因」与「恰好蒙对」。
    """
    original = pipeline_module.GenTxtRequest
    calls = {"n": 0}

    def _exploding(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == fail_at_stage:
            raise RuntimeError("模拟本地代码故障（非上游错误）")
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "GenTxtRequest", _exploding)

    client = http()
    async with client:
        pid = await _create_project(client)
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("x")]))
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]

        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"], "失败必须带可区分的 error_type"
        assert "Traceback" not in detail["error"], "用户文案不得含堆栈"

        # 归因（SC-005）：失败标记必须落在真正出错的阶段上
        summary = detail["summary"]
        assert summary.get("attempts") is not None, "通用兜底未补齐 attempts"
        assert "RuntimeError" in json.dumps(summary), "summary 未记录异常类名"

        steps = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps")
        ).json()["steps"]
        statuses = [s["status"] for s in steps]

        # FR-015 / INV-S2：不得残留 running 步骤
        assert "running" not in statuses, f"存在残留 running 步骤：{statuses}"
        assert statuses[fail_at_stage - 1] == "failed", (
            f"失败未归因到阶段 {fail_at_stage}（疑似仍硬编码到阶段 3）：{statuses}"
        )
        for i in range(fail_at_stage - 1):
            assert statuses[i] == "succeeded", f"阶段 {i + 1} 本应已完成：{statuses}"
        # 尚未开始的阶段不该被清扫波及（只有 running 才转 failed）
        for i in range(fail_at_stage, len(statuses)):
            assert statuses[i] == "pending", f"阶段 {i + 1} 未启动却被改写：{statuses}"


# ------------------------------------------------------------------ 失败记录完整性


def _fault_sequence(kind: str) -> list:
    """按故障类别构造上游响应序列（阶段 1、2 恒成功，故障从阶段 3 起）。"""
    if kind == "rate_limit":
        return [ANALYSIS_JSON, DESIGN_JSON] + [
            RateLimitError("429")
        ] * pipeline_module.MAX_ATTEMPTS
    if kind == "auth":
        return [ANALYSIS_JSON, DESIGN_JSON, PermissionDeniedError("403 insufficient balance")]
    if kind == "upstream_5xx":
        return [ANALYSIS_JSON, DESIGN_JSON] + [
            InternalServerError("503")
        ] * pipeline_module.MAX_ATTEMPTS
    if kind == "truncated":
        # 给足截断片段：够跑满「2 轮主调用 × 各 2 次续写」并有余量，
        # 免得因序列耗尽而失败成别的类别、把用例变成假绿。
        return [ANALYSIS_JSON, DESIGN_JSON] + [
            f"<!DOCTYPE html>\n<html>\n<body><p>半截{i}" for i in range(12)
        ]
    if kind == "empty":
        return [ANALYSIS_JSON, DESIGN_JSON] + [""] * pipeline_module.MAX_ATTEMPTS
    raise AssertionError(f"未知故障类别：{kind}")


FAULT_CASES = [
    ("rate_limit", "rate_limit"),
    ("auth", "auth"),
    ("upstream_5xx", "upstream_5xx"),
    ("truncated", "truncated"),
    ("empty", "empty"),
]


@pytest.mark.parametrize("kind,expected_error_type", FAULT_CASES)
async def test_every_fault_class_leaves_a_complete_failure_record(
    http, inject_fake_ai, kind, expected_error_type
):
    """五类故障都要落到「明确终态 + 完整记录」，且不留脏状态。

    FR-013 / FR-016 / FR-017 / SC-004。既有用例各自只断言了记录的一个片段
    （有的只查 error_type，有的只查 upstream_status），本用例补齐**共同不变量**：
    终态、用户可读且不含堆栈的文案、html 为空、无残留 running 步骤、未产生多余版本。
    这些恰恰是「失败之后用户还能立刻重来」的前提。
    """
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(FakeAIHub(_fault_sequence(kind)))
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]

        detail = (await _version_detail(client, pid, seq)).json()

        # —— 共同不变量 ——
        assert detail["status"] == "failed", f"{kind} 未落到终态"
        assert detail["error"], f"{kind} 缺少用户可读文案"
        assert "Traceback" not in detail["error"], f"{kind} 文案泄漏了堆栈"
        assert ".py" not in detail["error"], f"{kind} 文案泄漏了内部路径"
        assert detail["html"] == "", f"{kind} 失败版本不该有 html"
        assert detail["error_type"] == expected_error_type

        steps = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps")
        ).json()["steps"]
        statuses = [s["status"] for s in steps]
        assert "running" not in statuses, f"{kind} 残留 running 步骤：{statuses}"
        assert statuses[2] == "failed", f"{kind} 阶段 3 未标记失败：{statuses}"

        project = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
        assert project["version_count"] == 1, f"{kind} 产生了多余版本"
        assert project["latest_status"] == "failed"

        # —— 各类别额外要求的记录字段 ——
        summary = detail["summary"]
        if kind == "rate_limit":
            assert summary["attempts"] == pipeline_module.MAX_ATTEMPTS
            assert summary["upstream_status"] == 429
        elif kind == "auth":
            # 鉴权失败立即失败：只消耗 1 次尝试，不做无谓重试
            assert summary["attempts"] == 1, "鉴权失败不该重试"
            assert len(fake.requests) == 3, "鉴权失败后仍发起了额外调用"
        elif kind == "upstream_5xx":
            assert summary["upstream_status"] == 503

