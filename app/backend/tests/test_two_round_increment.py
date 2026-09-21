"""连续两轮增量上下文测试（设计文档 S4，验收 A4）。

验证第二轮生成时，模型实际收到的提示词里同时包含：
① 第一轮的原始需求（需求历史块，支撑「继续刚刚的需求」指代消解）；
② 第一轮产出的完整 HTML（增量改进基线）；
③ 失败/取消轮次的需求不进入历史（S1.2）。
断言依据是 FakeAIHub 记录的每一次 GenTxtRequest——注入内容的唯一可靠来源。
"""

from __future__ import annotations

from fakes import (
    ANALYSIS_JSON,
    DESIGN_JSON,
    FakeAIHub,
    PermissionDeniedError,
    make_html,
)
from services import prompts


async def _create_project(client) -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": "t"})
    return response.json()["public_id"]


async def _generate(client, pid: str, prompt: str):
    return await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": prompt}
    )


async def test_second_round_sees_history_and_previous_html(client, inject_fake_ai):
    pid = await _create_project(client)

    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("计算器v1")]))
    first = await _generate(client, pid, "做一个四则运算计算器")
    assert first.status_code == 202
    assert first.json()["version_seq"] == 1

    fake2 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("计算器v2")]))
    second = await _generate(
        client, pid, "继续刚刚的需求，加一个历史记录面板"
    )
    assert second.status_code == 202
    assert second.json()["version_seq"] == 2

    # 三个阶段的 user 消息都应带需求历史与指代消解提示
    for msg in fake2.user_messages():
        assert "需求历史" in msg
        assert "做一个四则运算计算器" in msg
    assert any("指代" in m or "刚才" in m for m in fake2.user_messages())

    # 代码阶段应回传上一版 HTML（增量基线），且保留 v1 标记
    code_msg = fake2.code_stage_messages()[0]
    assert "计算器v1" in code_msg
    assert "上一版页面的完整源码" in code_msg
    assert "历史记录面板" in code_msg

    # 第二轮成功后，最新版本可取回
    detail = (
        await client.get(f"/api/v1/atoms/projects/{pid}/versions/2")
    ).json()
    assert detail["status"] == "succeeded"
    assert "计算器v2" in detail["html"]


async def test_failed_round_prompt_not_in_history(client, inject_fake_ai):
    """失败轮次的需求不得污染后续轮次的需求历史（S1.2）。"""
    pid = await _create_project(client)

    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v1")]))
    await _generate(client, pid, "做一个番茄钟")

    # 第二轮：鉴权失败（不重试），需求「失败的指令」不应进入历史
    inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, PermissionDeniedError("403")])
    )
    second = await _generate(client, pid, "失败的指令")
    assert second.json()["version_seq"] == 2
    detail = (
        await client.get(f"/api/v1/atoms/projects/{pid}/versions/2")
    ).json()
    assert detail["status"] == "failed"

    fake3 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v3")]))
    third = await _generate(client, pid, "再加一个统计图")
    assert third.json()["version_seq"] == 3

    history_msgs = fake3.user_messages()
    assert all("做一个番茄钟" in m for m in history_msgs)   # 成功轮次在历史里
    assert all("失败的指令" not in m for m in history_msgs)  # 失败轮次被排除
    # 基线仍是 v1（失败版本不成为 previous_html）
    code_msg = fake3.code_stage_messages()[0]
    assert "v1" in code_msg and "v2" not in code_msg


async def test_history_budget_does_not_grow_linearly(client, inject_fake_ai):
    """多轮后历史块受 HISTORY_MAX_ITEMS 条数上限约束，上下文不线性膨胀。"""
    pid = await _create_project(client)
    for i in range(1, prompts.HISTORY_MAX_ITEMS + 4):  # 超出上限 3 轮
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html(f"r{i}")]))
        await _generate(client, pid, f"第{i}轮需求内容")

    fake_last = inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("final")])
    )
    await _generate(client, pid, "最后一轮")
    msg = fake_last.user_messages()[0]
    # 当前需求只出现一次（作为本次要求），不在历史块里重复
    assert msg.count("最后一轮") == 1
    assert "第1轮需求内容" not in msg  # 最旧的已被裁掉
    assert f"第{prompts.HISTORY_MAX_ITEMS + 3}轮需求内容" in msg  # 最新的保留
