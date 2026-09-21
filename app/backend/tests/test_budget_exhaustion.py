"""整体预算耗尽的表现与分类（FR-007/009/013/018/019，contracts/rest-api.md 变更 5）。

变更 5 的契约要求里最容易被写成空转的一条是「``error_type`` 取一个**可区分**于
上游超时的值」——只断言各自字面量无法证明二者被区分。本文件用**两种注入**把
区分做成可执行的断言：

1. **上游永不返回**（平台从上游拿不到任何响应）→ ``timeout``。这是上游不响应，
   用户应当稍后重试。修复前的形态是「6 阶段 × 3 次尝试 × 240s ≈ 72 分钟」，
   而前端只等 12 分钟；这里用调用次数与耗时同时锁死它已被整体预算收敛。
2. **预算在阶段边界被吃光**（上一阶段成功但很慢）→ ``budget_exhausted``。这是
   平台侧主动止损，用户应当精简需求。注意这是**唯一**能真正「超出预算」的形态：
   预算被**成功**的调用吃掉，而不是被失败的调用浪费掉（后者最后一次尝试的
   失败原因才是事实，报 ``timeout`` 是诚实的，不该改写成预算耗尽）。

两个场景的 ``error_type`` 与 ``error`` 文案都必须**不相等**（见
``test_budget_stop_loss_is_distinguishable_from_upstream_silence``）——用户动作
不同决定了它们不能是同一句话。

常量一律按秒级缩放（真实 ``GENERATION_BUDGET_SECONDS=420`` 不可等）：三个预算
常量必须**一起**缩放，只缩小总预算会让单次调用上限相对变大，场景 1 的
「单次等待被剩余预算卡住」就不再成立。
"""

from __future__ import annotations

import asyncio
import time

from sqlalchemy import select

from core.database import db_manager
from fakes import ANALYSIS_JSON, DESIGN_JSON, FakeAIHub, make_html
from models.versions import Versions
from services import pipeline as pipeline_module

PROMPT_TEXT = "做一个记录每日饮水的计数器"


def _shrink_budget(
    monkeypatch, *, budget: float, stage_timeout: float, min_call: float
) -> None:
    """把预算链缩放到秒级，使测试不必等真实的 420s。"""
    monkeypatch.setattr(pipeline_module, "GENERATION_BUDGET_SECONDS", budget)
    monkeypatch.setattr(pipeline_module, "STAGE_TIMEOUT", stage_timeout)
    monkeypatch.setattr(pipeline_module, "MIN_CALL_BUDGET_SECONDS", min_call)


async def _never_returns(_request):
    """永不返回的上游：只有 ``wait_for`` 的取消能让它停下来。"""
    await asyncio.Event().wait()


async def _make_project(client, title: str = "预算") -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": title})
    assert response.status_code in (200, 201), response.text
    return response.json()["public_id"]


async def _generate(client, pid: str):
    return await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": PROMPT_TEXT}
    )


async def _snapshot(client, pid: str, seq: int = 1) -> dict:
    response = await client.get(
        f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps"
    )
    return response.json()


async def _detail(client, pid: str, seq: int = 1) -> dict:
    response = await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}")
    return response.json()


async def _version_count(pid: str) -> int:
    async with db_manager.session() as session:
        rows = (
            await session.execute(
                select(Versions).where(Versions.project_public_id == pid)
            )
        ).scalars().all()
    return len(rows)


async def _scenario_upstream_silence(client, inject_fake_ai, monkeypatch) -> dict:
    """上游永不返回：返回耗时、调用次数与终态快照。

    客户端由调用方持有并保持打开——场景跑完还要用同一个访客再次提交（FR-018）。
    """
    _shrink_budget(monkeypatch, budget=0.6, stage_timeout=0.25, min_call=0.05)
    fake = inject_fake_ai(FakeAIHub([_never_returns] * 20))

    pid = await _make_project(client)
    started = time.monotonic()
    accepted = await _generate(client, pid)
    elapsed = time.monotonic() - started
    assert accepted.status_code == 202, accepted.text

    return {
        "pid": pid,
        "elapsed": elapsed,
        "calls": len(fake.requests),
        "detail": await _detail(client, pid),
        "snap": await _snapshot(client, pid),
    }


async def _scenario_budget_drained_by_slow_stage(
    client, inject_fake_ai, monkeypatch
) -> dict:
    """阶段 1 成功但很慢，把预算吃到阶段 2 无法发起调用。"""
    budget, delay = 1.2, 0.9
    _shrink_budget(monkeypatch, budget=budget, stage_timeout=budget, min_call=0.5)

    async def _slow_analysis(_request):
        await asyncio.sleep(delay)
        return ANALYSIS_JSON

    fake = inject_fake_ai(FakeAIHub([_slow_analysis, DESIGN_JSON, make_html("预算")]))

    pid = await _make_project(client)
    accepted = await _generate(client, pid)
    assert accepted.status_code == 202, accepted.text

    return {
        "pid": pid,
        "delay": delay,
        "calls": len(fake.requests),
        "detail": await _detail(client, pid),
        "snap": await _snapshot(client, pid),
    }


async def test_never_returning_upstream_is_bounded_by_budget(
    http, inject_fake_ai, monkeypatch
):
    """FR-007/009：上游永不返回也必须落在预算附近，而不是 6 倍预算。"""
    async with http() as client:
        result = await _scenario_upstream_silence(client, inject_fake_ai, monkeypatch)
        detail, snap = result["detail"], result["snap"]

        assert snap["status"] == "failed"
        assert detail["status"] == "failed"

        # 单次等待取 min(STAGE_TIMEOUT, 剩余预算)，重试次数取 MAX_ATTEMPTS：
        # 调用次数与耗时同时锁死「逐次生效」的老形态（6 阶段 × 3 次 = 18 次）。
        assert result["calls"] == pipeline_module.MAX_ATTEMPTS, (
            f"上游调用次数 {result['calls']}，不是按整体预算收敛的形态"
        )
        # 容忍度 = 退避总和 + 调度余量（DB 提交、事件循环唤醒）。
        tolerance = sum(pipeline_module.RETRY_BACKOFF_SECONDS) + 0.3
        budget = pipeline_module.GENERATION_BUDGET_SECONDS
        assert result["elapsed"] <= budget + tolerance, (
            f"耗时 {result['elapsed']:.2f}s 超出预算 {budget}s + 容忍度 {tolerance}s"
        )

        # 期间没有任何阶段启动过：阶段 1 自己就失败了（FR-014 的真实归因）。
        assert [s["status"] for s in snap["steps"]] == ["failed", "pending", "pending"]
        assert all(s["status"] != "running" for s in snap["steps"]), "残留 running 步骤"

        # 分类是「上游不响应」，不是平台侧止损：平台并没有因为预算不足放弃过调用，
        # 是真的等了上游两次而它没答。
        assert detail["error_type"] == "timeout"
        assert detail["summary"]["attempts"] == pipeline_module.MAX_ATTEMPTS
        assert detail["summary"].get("upstream_status") is None

        # 失败记录完整性（变更 4）：可读、无堆栈与内部路径。
        assert "模型服务" in detail["error"]
        assert "Traceback" not in detail["error"]
        assert ".py" not in detail["error"]

        # html 为空 + 描述保留（FR-019）+ 无多余版本。
        assert detail["html"] == ""
        assert detail["prompt"] == PROMPT_TEXT
        assert await _version_count(result["pid"]) == 1

        # FR-018：失败之后立即可再次提交，且项目未被这次失败污染。
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("重来")]))
        retry = await _generate(client, result["pid"])
        assert retry.status_code == 202, retry.text
        assert retry.json()["version_seq"] == 2
        assert (await _detail(client, result["pid"], 2))["status"] == "succeeded"


async def test_budget_drained_at_stage_boundary_is_platform_stop_loss(
    http, inject_fake_ai, monkeypatch
):
    """FR-007/013：预算被慢而成功的阶段吃光时，以可理解的止损落库。"""
    async with http() as client:
        result = await _scenario_budget_drained_by_slow_stage(
            client, inject_fake_ai, monkeypatch
        )
        detail, snap = result["detail"], result["snap"]

        assert snap["status"] == "failed"
        assert detail["error_type"] == pipeline_module.BUDGET_EXHAUSTED_ERROR_TYPE
        # 文案说明「超出时间预算」与「描述已保留，可重新提交」（变更 5 契约要求）。
        assert "超出时间预算" in detail["error"]
        assert "描述已保留" in detail["error"]

        # 止损发生在阶段边界：阶段 1 已完成（有效的中间产物，不得被改写），
        # 阶段 2 根本没发起调用，因此仍是 pending——归因到真正被止损的那一步，
        # 且没有任何步骤停在 running（FR-014/FR-015）。
        assert [s["status"] for s in snap["steps"]] == [
            "succeeded",
            "failed",
            "pending",
        ]
        # 尝试次数必须如实记录为 0：一次上游调用都没发生。缺失该字段等于把
        # 「没试过」和「试过失败了」混为一谈（FR-013）。
        assert detail["summary"]["attempts"] == 0
        assert result["calls"] == 1, "阶段 2 不该向已耗尽的预算再发起调用"

        assert detail["html"] == ""
        assert detail["prompt"] == PROMPT_TEXT
        assert await _version_count(result["pid"]) == 1, "止损过程产生了新版本"

        # FR-018 + 变更 5 验收断言 3：紧接着再次 generate 必须被受理。
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("重来")]))
        retry = await _generate(client, result["pid"])
        assert retry.status_code == 202, retry.text
        assert retry.json()["version_seq"] == 2
        assert (await _detail(client, result["pid"], 2))["status"] == "succeeded"


async def test_budget_stop_loss_is_distinguishable_from_upstream_silence(
    http, inject_fake_ai, monkeypatch
):
    """「可区分」的唯一可执行检验：两种注入的 ``error_type`` 与文案都不相同。

    区分是必需的而非修辞：``timeout`` 要求用户稍后重试（上游的问题会自己好），
    ``budget_exhausted`` 要求用户精简需求（重试同样会超预算）。把两者报成同一
    句话，用户就会一直重试一个注定失败的请求。
    """
    async with http() as client:
        silence = await _scenario_upstream_silence(client, inject_fake_ai, monkeypatch)
        stop_loss = await _scenario_budget_drained_by_slow_stage(
            client, inject_fake_ai, monkeypatch
        )

        silence_type = silence["detail"]["error_type"]
        stop_loss_type = stop_loss["detail"]["error_type"]
        assert silence_type != stop_loss_type

        # 前端拿到的是一个非空、可枚举、与文案一一对应的分类值（FR-017 的前提）。
        assert silence_type and stop_loss_type
        assert (
            silence["detail"]["error"] != stop_loss["detail"]["error"]
        ), "两种失败给出了同一句用户文案，用户无法据以决定下一步"

        # 两条文案都必须能被用户读懂：不给堆栈、不给内部路径。
        for detail in (silence["detail"], stop_loss["detail"]):
            assert "Traceback" not in detail["error"]
            assert ".py" not in detail["error"]
            assert "/" not in detail["error"]
