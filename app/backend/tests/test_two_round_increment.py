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
from services import pipeline as pipeline_module
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


# ------------------------------------------------- 增量基线：最近一个**成功**版本


async def test_baseline_is_round1_output_when_round2_failed(client, inject_fake_ai):
    """第 1 轮成功、第 2 轮失败、第 3 轮请求：基线是第 1 轮的产出（且非空）。

    对应 FR-002：失败轮次不得成为增量基线。若实现改成「取 seq 最大的版本」而
    不筛状态，第 3 轮就会拿到第 2 轮那版（html 为空），产物会从零重做——
    用户看到的现象是「追加一句需求，旧功能全没了」。
    """
    pid = await _create_project(client)

    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("基线v1")]))
    await _generate(client, pid, "做一个四则运算计算器")

    # 第 2 轮：阶段 3 鉴权失败（不重试），版本落 failed、html 为空
    inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, PermissionDeniedError("403")])
    )
    failed = await _generate(client, pid, "这一轮会失败")
    second_seq = failed.json()["version_seq"]
    second = (
        await client.get(f"/api/v1/atoms/projects/{pid}/versions/{second_seq}")
    ).json()
    assert second["status"] == "failed"
    assert not second["html"], "失败版本不应有 html——否则它可能成为基线"

    fake3 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v3")]))
    await _generate(client, pid, "再加一个历史记录面板")

    code_msg = fake3.code_stage_messages()[0]
    assert "上一版页面的完整源码" in code_msg
    assert "基线v1" in code_msg, "基线必须是第 1 轮的产出"
    assert "这一轮会失败" not in code_msg, "失败轮次的需求不得进入历史"
    assert "再加一个历史记录面板" in code_msg  # 本次需求与基线同时在场


async def test_baseline_is_newest_succeeded_not_oldest(client, inject_fake_ai):
    """两轮都成功时，基线必须是**较新**那一版，而不是最早的成功版本。

    这条区分「最近一个成功版本」与「第一个成功版本」——只看「非失败」状态的
    选择逻辑（例如误用升序取首个）会通过上一条测试，却在这里露馅。
    """
    pid = await _create_project(client)

    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("第1版")]))
    await _generate(client, pid, "第一轮需求")

    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("第2版")]))
    await _generate(client, pid, "第二轮需求")

    fake3 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("第3版")]))
    await _generate(client, pid, "第三轮需求")

    code_msg = fake3.code_stage_messages()[0]
    assert "第2版" in code_msg, "基线应是最近一个成功版本"
    assert "第1版" not in code_msg, "不得回退到更早的成功版本"
    # 历史按时间升序，两条成功的需求都在
    assert "第一轮需求" in code_msg and "第二轮需求" in code_msg


# ------------------------------------------------- 指代消解提示真的被注入


async def test_anaphora_hint_reaches_the_stages_that_see_the_raw_prompt(
    client, inject_fake_ai
):
    """指代消解提示（ANAPHORA_HINT）必须真的注入到**直接读原始需求文本**的阶段。

    这是 spec 点名的缺口：``ANAPHORA_HINT`` 在 ``tests/`` 下原本零引用，也就是
    「模型能理解『继续刚刚的需求』」这件事从未被断言过。只断言历史块存在不够——
    历史块提供了事实，ANAPHORA_HINT 提供的是「把这些事实用于消解指代」的指令，
    两者缺一，模型都可能把「继续刚刚的需求」当成孤立的新需求。

    **为什么只断言阶段 1 与阶段 3**（而不是三个阶段）：这是实现里刻意的分工，
    不是遗漏，也正因如此不该靠改实现来迁就一个更宽的断言。

    - 阶段 1 拿到的是**原始需求文本**——「继续刚刚的需求」这句指代在这里第一次
      出现，必须由它消解，所以历史块 + ANAPHORA_HINT 都在。
    - 阶段 3 拿到的又是**原始需求文本**（代码就是照它写的），且新增了上一版
      HTML；指代若在这里被误读成孤立需求，产物会直接丢失旧功能，所以同样两者都在。
    - 阶段 2 是唯一一个**不直接读原始需求、而读阶段 1 产出的分析结果**的阶段：
      指代在阶段 1 已经消解完毕，并以「阶段一的需求分析结果」的形式作为阶段 2
      的输入。它的「这是延续不是重来」由 ``build_design_user`` 里「请设计在其
      基础上改进的结构，明确指出哪些区域保留、哪些区域新增」这段迭代框架承担。

    因此本用例改成「按理由断言」：既锁定阶段 1/3 真的有提示，也锁定阶段 2 真的
    持有那个让提示变得多余的替代物（分析结果 + 迭代框架）。若有人把分析结果从
    阶段 2 抽走，或者把阶段 1/3 的提示删掉，本用例都会红。
    """
    # 常量本身要有实质内容（防止它被清空后测试依然「通过」）
    assert len(prompts.ANAPHORA_HINT.strip()) > 30
    assert "指代" in prompts.ANAPHORA_HINT

    pid = await _create_project(client)
    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v1")]))
    await _generate(client, pid, "做一个四则运算计算器")

    fake2 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v2")]))
    await _generate(client, pid, "继续刚刚的需求，加一个历史记录面板")

    messages = fake2.user_messages()
    assert len(messages) == 3, "三个阶段各一次调用"
    analyze_msg, design_msg, code_msg = messages  # user_messages() 按调用顺序返回

    # 三个阶段的共同前提：指代所依赖的事实（历史块）必须都在场。
    # 只有提示没有事实，模型无从消解；只有事实没有提示，模型可能不去消解。
    for index, msg in enumerate(messages, start=1):
        assert "需求历史" in msg, f"阶段 {index} 未注入需求历史"
        assert "做一个四则运算计算器" in msg, f"阶段 {index} 的历史里缺第 1 轮需求"

    # 读原始需求文本的两个阶段：必须有消解指令
    assert prompts.ANAPHORA_HINT in analyze_msg, "阶段 1 未注入指代消解提示"
    assert prompts.ANAPHORA_HINT in code_msg, "阶段 3 未注入指代消解提示"

    # 阶段 2：提示缺席，但替代物必须在场——阶段 1 消解后的分析结果 + 迭代框架。
    # 这两条才是「阶段 2 不需要提示」这一判断的依据；缺了它们，上面那条缺席
    # 就从「设计」退化成「遗漏」。
    assert prompts.ANAPHORA_HINT not in design_msg, (
        "阶段 2 引入了指代消解提示——若这是有意的改动，请同步更新本用例的理由段"
    )
    assert "阶段一的需求分析结果" in design_msg, (
        "阶段 2 既没有指代消解提示，又没有阶段 1 的解析结果——它无从知道本次需求是延续"
    )
    assert "测试应用" in design_msg, "阶段 2 未收到阶段 1 分析结果的实际内容"
    assert "这是一次迭代" in design_msg and "保留" in design_msg, (
        "阶段 2 未被告知「在既有结构上改进、说明哪些区域保留」——这正是它承接延续性的方式"
    )


# ------------------------------------------------- 上下文体积上限（S3.3 预算闸门）
#
# 这四个符号（PREVIOUS_HTML_MAX_CHARS / truncate_previous_html /
# CONTINUE_HISTORY_MAX_CHARS / truncate_continue_history）原先在 tests/ 下零引用，
# 也就是「上下文不随轮次线性膨胀」这条承诺从未被断言过。这里既测纯函数本身，
# 也测它们在真实流水线里确实生效（只测纯函数会漏掉「函数写对了但没被调用」）。


def test_previous_html_under_cap_is_injected_verbatim():
    html = make_html("小页面")
    assert len(html) <= prompts.PREVIOUS_HTML_MAX_CHARS
    assert prompts.truncate_previous_html(html) == html


def test_previous_html_over_cap_keeps_head_and_tail():
    head = "<!DOCTYPE html>\n<html>\n<head><title>T</title></head>\n<body>\n"
    body = "<p>中间内容占位</p>\n" * 4000
    tail = "<p>末尾标记TAIL</p>\n</body>\n</html>"
    html = head + body + tail
    assert len(html) > prompts.PREVIOUS_HTML_MAX_CHARS  # 前置条件：确实超限

    out = prompts.truncate_previous_html(html)
    # 上限 + 省略标记的余量
    assert len(out) <= prompts.PREVIOUS_HTML_MAX_CHARS + 100
    assert out.startswith("<!DOCTYPE html>"), "文档头必须保留"
    assert "</head>" in out, "head 段必须保留（外部依赖与样式集中在这里）"
    assert "末尾标记TAIL" in out, "尾部必须保留（页面主体逻辑多在末尾）"
    assert "已省略" in out, "必须显式告知模型内容被裁过"


def test_previous_html_handles_absent_values():
    assert prompts.truncate_previous_html(None) == ""
    assert prompts.truncate_previous_html("") == ""


def test_continue_history_under_cap_passes_through():
    doc = "x" * (prompts.CONTINUE_HISTORY_MAX_CHARS - 1)
    assert prompts.truncate_continue_history(doc) == doc


def test_continue_history_over_cap_keeps_only_the_tail():
    doc = "".join(str(i % 10) for i in range(prompts.CONTINUE_HISTORY_MAX_CHARS * 3))
    out = prompts.truncate_continue_history(doc)
    assert len(out) <= prompts.CONTINUE_HISTORY_MAX_CHARS + 30
    assert out.endswith(doc[-200:]), "续写需要的正是尾部，不能被裁掉"
    assert "尾部片段" in out, "必须显式告知模型这是片段"


def test_continue_history_cap_covers_the_continuation_tail():
    """续写历史上限必须大于 pipeline 回传的中断点片段长度。

    否则「模型看到的续写点上下文」比「我们让它接着写的位置」还短，
    续写会从头重来或重复输出。
    """
    assert prompts.CONTINUE_HISTORY_MAX_CHARS > pipeline_module.CONTINUE_TAIL_CHARS


async def test_oversized_previous_html_is_bounded_in_injected_prompt(
    client, inject_fake_ai
):
    """产物级：上一版 HTML 远超上限时，真正注入给模型的消息体积仍受约束。"""
    pid = await _create_project(client)
    huge = (
        "<!DOCTYPE html>\n<html>\n<head><title>巨大页面</title></head>\n<body>\n"
        + "<p>内容占位</p>\n" * 20_000
        + "</body>\n</html>"
    )
    assert len(huge) > prompts.PREVIOUS_HTML_MAX_CHARS

    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, huge]))
    first = await _generate(client, pid, "做一个内容很多的页面")
    assert first.status_code == 202
    # 确认这一轮真的成功了（失败版本不会有 html，基线断言就失去意义）
    detail = (await client.get(f"/api/v1/atoms/projects/{pid}/versions/1")).json()
    assert detail["status"] == "succeeded", detail
    assert len(detail["html"]) > prompts.PREVIOUS_HTML_MAX_CHARS

    fake2 = inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v2")]))
    await _generate(client, pid, "继续优化一下样式")

    code_msg = fake2.code_stage_messages()[0]
    assert "已省略" in code_msg, "超限基线应触发截断"
    assert len(code_msg) < len(huge), "不得把上一版原始 HTML 整段塞进提示词"
    # 上界：截断后的基线 + 固定文案，与原始长度无关
    assert len(code_msg) < prompts.PREVIOUS_HTML_MAX_CHARS + 10_000
