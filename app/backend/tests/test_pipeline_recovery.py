"""生成链路故障恢复测试（设计文档 S3 / S4，验收 A3）。

GENERATION_INLINE=1 让受理接口在当前协程内跑完流水线：post(generate)
返回时版本已处于终态，无需轮询，测试确定且快速。
"""

from __future__ import annotations

import asyncio

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
    """429 一次后重试成功：版本终态 succeeded，共 4 次调用（T003 收紧为最多 2 次尝试）。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub(
                [ANALYSIS_JSON, DESIGN_JSON, RateLimitError("429"), make_html("x")]
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        assert (await _version_detail(client, pid, seq)).json()["status"] == "succeeded"
        assert len(fake.requests) == 4


async def test_rate_limit_exhausted_marks_failed(http, inject_fake_ai):
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub(
                [ANALYSIS_JSON, DESIGN_JSON] + [RateLimitError("429")] * 2
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "rate_limit"  # S3.5 可观测
        assert detail["summary"]["upstream_status"] == 429
        assert detail["summary"]["attempts"] == 2
        assert "限流" in detail["error"]  # 面向用户的可读中文
        assert len(fake.requests) == 4  # 1 + 2 次尝试
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
                [ANALYSIS_JSON, DESIGN_JSON] + [lambda req: slow()] * 3
            )
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        detail = (await _version_detail(client, pid, seq)).json()
        assert detail["status"] == "failed"
        assert detail["error_type"] == "timeout"
        assert "超时" in detail["error"]


# ------------------------------------------------------------------ 空内容降级


async def test_empty_content_retries_with_lower_max_tokens(http, inject_fake_ai):
    """首次空内容后降级 max_tokens（16384→8192）重试并成功（最多 2 次尝试）。"""
    client = http()
    async with client:
        pid = await _create_project(client)
        fake = inject_fake_ai(
            FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, "   ", make_html("ok")])
        )
        seq = (await _generate(client, pid, "做一个待办")).json()["version_seq"]
        assert (await _version_detail(client, pid, seq)).json()["status"] == "succeeded"
        assert len(fake.requests) == 4
        assert fake.requests[2].max_tokens == 16384  # 第一次代码调用
        assert fake.requests[3].max_tokens == 8192  # 降级后


async def test_all_empty_content_fails_as_empty(http, inject_fake_ai):
    client = http()
    async with client:
        pid = await _create_project(client)
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, "", "", ""]))
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
