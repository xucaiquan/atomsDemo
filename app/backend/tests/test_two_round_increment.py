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
    RateLimitError,
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


# ------------------------------------------------------------------ T009 增量基线取最近成功版本


async def test_increment_baseline_is_latest_succeeded_version(client, inject_fake_ai):
    """第 1 轮成功、第 2 轮失败后，第 3 轮注入的 previous_html 仍是第 1 轮产出（FR-002）。

    失败/取消版本不得进入候选：既不能成为基线（第 2 轮没有产出），也不能
    让基线回退为空——模型必须拿到第 1 轮的真实页面做增量。
    """
    pid = await _create_project(client)

    # 第 1 轮成功，产出带唯一标记的 HTML
    inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("第一轮基线产物")])
    )
    first = await _generate(client, pid, "做一个四则运算计算器")
    assert first.json()["version_seq"] == 1

    # 第 2 轮在代码阶段连续 429（最多 2 次尝试后耗尽）→ failed，无产出
    inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON] + [RateLimitError("429")] * 2)
    )
    second = await _generate(client, pid, "第二轮注定失败的需求")
    assert second.json()["version_seq"] == 2
    detail2 = (await client.get(f"/api/v1/atoms/projects/{pid}/versions/2")).json()
    assert detail2["status"] == "failed"
    assert not detail2.get("html")

    # 第 3 轮：基线必须是第 1 轮的真实产出（非空、非第 2 轮痕迹）
    fake3 = inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("第三轮产物")])
    )
    third = await _generate(client, pid, "继续刚刚的需求，加一个统计面板")
    assert third.json()["version_seq"] == 3
    code_msg = fake3.code_stage_messages()[0]
    assert "上一版页面的完整源码" in code_msg  # 基线非空，确实注入了页面
    assert "第一轮基线产物" in code_msg  # 来自第 1 轮
    assert "第二轮注定失败的需求" not in code_msg  # 第 2 轮（失败）未进入


# ------------------------------------------------------------------ T010 指代消解提示注入


async def test_anaphora_hint_injected_on_increment_rounds(client, inject_fake_ai):
    """迭代轮次的消息同时包含需求历史块与 ANAPHORA_HINT 指示内容。

    spec 第 8 条点名的缺口：ANAPHORA_HINT 常量此前在 tests/ 下零引用，
    无法证明它真实进入了模型上下文。
    """
    pid = await _create_project(client)
    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v1")]))
    await _generate(client, pid, "做一个记账本")

    fake2 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v2")]))
    await _generate(client, pid, "继续刚刚的需求，加一个月度统计")

    msgs = fake2.user_messages()  # [需求分析, 结构设计, 代码生成] 各一条
    assert len(msgs) == 3
    hint_marker = "请结合上面的需求历史理解它的真实含义"
    assert hint_marker in prompts.ANAPHORA_HINT  # 断言依据即该常量本身
    for m in msgs:
        assert "需求历史" in m  # 历史上下文块贯穿三阶段
        assert "做一个记账本" in m
    assert hint_marker in msgs[0]  # 阶段 1 需求分析：迭代语义提示
    assert hint_marker in msgs[2]  # 阶段 3 代码生成：迭代语义提示


# ------------------------------------------------------------------ T011 上下文体积上限


def _big_html(marker: str, total_chars: int) -> str:
    """体积可控、结构完整（DOCTYPE+</html>）的上一版页面。"""
    filler = "x" * max(0, total_chars - 300)
    return (
        f"<!DOCTYPE html>\n<html>\n<head><style>/*{filler}*/</style>"
        f"<title>{marker}</title></head>\n<body><p>{marker}</p></body>\n</html>"
    )


def test_truncate_previous_html_caps_volume():
    """PREVIOUS_HTML_MAX_CHARS 闸门：未超限原样返回，超限保留头尾并插入省略标记。"""
    short = make_html("小页面")
    assert prompts.truncate_previous_html(short) == short

    huge = _big_html("大页面", prompts.PREVIOUS_HTML_MAX_CHARS * 3)
    truncated = prompts.truncate_previous_html(huge)
    assert len(truncated) < len(huge)
    assert len(truncated) <= prompts.PREVIOUS_HTML_MAX_CHARS + 100
    assert "已省略" in truncated
    assert truncated.startswith("<!DOCTYPE html>")
    assert truncated.rstrip().endswith("</html>")
    assert "大页面" in truncated  # 头部标记保留


def test_truncate_continue_history_caps_volume():
    """CONTINUE_HISTORY_MAX_CHARS 闸门：超长续写历史只回传尾部片段。"""
    short = "a" * 100
    assert prompts.truncate_continue_history(short) == short

    huge = "b" * (prompts.CONTINUE_HISTORY_MAX_CHARS + 5000)
    truncated = prompts.truncate_continue_history(huge)
    assert len(truncated) <= prompts.CONTINUE_HISTORY_MAX_CHARS + 40
    assert "尾部片段" in truncated


async def test_previous_html_budget_applies_to_prompt(client, inject_fake_ai):
    """超长上一版 HTML 在注入提示词前被裁剪；多轮迭代后上下文体积保持有界（FR-005）。"""
    pid = await _create_project(client)

    # 第 1 轮产出 3 倍预算体积的超大页面
    inject_fake_ai(
        FakeAIHub(
            [
                ANALYSIS_JSON,
                DESIGN_JSON,
                _big_html("超大页面", prompts.PREVIOUS_HTML_MAX_CHARS * 3),
            ]
        )
    )
    await _generate(client, pid, "做一个数据看板")

    fake2 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v2")]))
    await _generate(client, pid, "继续优化样式")
    code_msg = fake2.code_stage_messages()[0]
    assert "已省略" in code_msg  # 预算闸门在真实请求里生效
    # 消息整体有界：截断后的 HTML + 阶段脚手架，不随上一版体积无限膨胀
    assert len(code_msg) <= prompts.PREVIOUS_HTML_MAX_CHARS + 4000

    # 再迭代 4 轮后，代码阶段消息长度仍有界（需求历史受 HISTORY_MAX_ITEMS 约束）
    for i in range(3, 7):
        inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html(f"r{i}")]))
        await _generate(client, pid, f"第{i}轮微调")
    fake_last = inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("final")])
    )
    await _generate(client, pid, "最后一轮微调")
    last_code_msg = fake_last.code_stage_messages()[0]
    assert len(last_code_msg) <= prompts.PREVIOUS_HTML_MAX_CHARS + 4000
