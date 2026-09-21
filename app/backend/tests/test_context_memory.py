"""上下文记忆与截断续写的纯函数单元测试。

覆盖本轮「优化上下文记忆 + 任务可中断取消」增强的可离线验证部分：
- prompts.build_history_block：历史需求拼接、条数与单条长度截断
- prompts.build_analyze_user / build_code_user：指代消解提示与上一版 HTML 注入
- pipeline._merge_continuation：续写片段去重叠拼接
- pipeline._fallback_title：标题回退逻辑
"""

from __future__ import annotations

import pytest

from services import prompts
from services.pipeline import _fallback_title, _merge_continuation


# ---------- build_history_block ----------


def test_history_block_empty():
    assert prompts.build_history_block(None) == ""
    assert prompts.build_history_block([]) == ""
    assert prompts.build_history_block(["", "   "]) == ""


def test_history_block_lists_items_in_order():
    block = prompts.build_history_block(["做一个计算器", "加历史记录"])
    assert "需求历史" in block
    assert "1. 做一个计算器" in block
    assert "2. 加历史记录" in block
    assert block.index("做一个计算器") < block.index("加历史记录")


def test_history_block_caps_item_count():
    import re

    items = [f"需求{i}" for i in range(1, 12)]  # 11 条
    block = prompts.build_history_block(items)
    # 只保留最近 HISTORY_MAX_ITEMS 条，且编号从 1 重排
    numbered = [l for l in block.splitlines() if re.match(r"^\d+\. ", l)]
    assert len(numbered) == prompts.HISTORY_MAX_ITEMS
    assert numbered[0].startswith("1. 需求6")  # 最旧的 5 条被丢弃
    assert numbered[-1].endswith("需求11")
    assert "需求5" not in block


def test_history_block_truncates_older_item():
    """非最新条目按 HISTORY_ITEM_CHARS 截断（S3.3）。"""
    long_prompt = "字" * (prompts.HISTORY_ITEM_CHARS + 500)
    block = prompts.build_history_block([long_prompt, "收尾需求"])
    assert "字" * prompts.HISTORY_ITEM_CHARS in block
    assert "字" * (prompts.HISTORY_ITEM_CHARS + 1) not in block


def test_history_block_latest_item_gets_larger_budget():
    """最新一条放宽到 HISTORY_LATEST_ITEM_CHARS：长需求里的关键约束不被裁掉。"""
    long_prompt = "字" * (prompts.HISTORY_LATEST_ITEM_CHARS + 500)
    block = prompts.build_history_block(["旧需求", long_prompt])
    assert "字" * prompts.HISTORY_LATEST_ITEM_CHARS in block
    assert "字" * (prompts.HISTORY_LATEST_ITEM_CHARS + 1) not in block


# ---------- 指代消解：分析 / 代码阶段用户消息 ----------


def test_analyze_user_plain_prompt_no_history():
    msg = prompts.build_analyze_user("做一个待办清单")
    assert msg == "用户的应用需求描述：\n做一个待办清单"
    assert "需求历史" not in msg


def test_analyze_user_with_history_includes_anaphora_hint():
    msg = prompts.build_analyze_user(
        "继续刚刚的需求", history_prompts=["做一个每日菜谱推荐应用"]
    )
    assert "需求历史" in msg
    assert "每日菜谱推荐" in msg
    assert "继续刚刚的需求" in msg
    # 指代消解提示必须出现，模型才能把短句还原为对既有应用的延续
    assert "指代" in msg or "刚才" in msg


def test_analyze_user_with_previous_html_marks_iteration():
    msg = prompts.build_analyze_user("加个深色模式", previous_html="<!DOCTYPE html><html></html>")
    assert "迭代" in msg
    assert "保留已有功能" in msg


def test_code_user_contains_history_hint_and_previous_html():
    msg = prompts.build_code_user(
        "按之前说的再加一个排行榜",
        analysis='{"app_name":"贪吃蛇"}',
        design='{"layout":"游戏区+侧栏"}',
        previous_html="<html>OLD-CALCULATOR-MARKER</html>",
        history_prompts=["做一个贪吃蛇游戏", "加计分板"],
    )
    assert "需求历史" in msg
    assert "贪吃蛇游戏" in msg and "计分板" in msg
    assert "OLD-CALCULATOR-MARKER" in msg  # 上一版源码回传
    # 措辞升级：原先只有一句「保留已有功能」，实测不足以阻止模型改写旧按钮文案，
    # 现改为注入成块的硬性保留约束（见 test_two_round_increment 的退化用例）。
    assert prompts.INCREMENT_PRESERVE_RULES in msg
    assert msg.rstrip().endswith("现在请输出完整的 HTML 源码。")


def test_code_user_first_generation_without_context():
    msg = prompts.build_code_user("做一个番茄钟", analysis="A", design="D")
    # 无历史时不注入「需求历史」块（指代提示语本身常驻，但历史标题不出现）
    assert "【本项目此前的需求历史" not in msg
    assert "上一版页面" not in msg
    assert "做一个番茄钟" in msg


# ---------- 截断续写合并 ----------


def test_merge_continuation_removes_overlap():
    existing = "<html><body>score = 10; ren"
    continuation = "render();</body></html>"
    merged = _merge_continuation(existing, continuation)
    assert merged == "<html><body>score = 10; render();</body></html>"
    assert "render" in merged and merged.count("render();") == 1


def test_merge_continuation_repeats_head_fully():
    # 模型复述了中断点前的一整段文字，重叠应被去除
    existing = "<html>ABC"
    continuation = "ABCDEF</html>"
    merged = _merge_continuation(existing, continuation)
    assert merged == "<html>ABCDEF</html>"


def test_merge_continuation_no_overlap_appends():
    existing = "<html>AAA"
    continuation = "BBB</html>"
    merged = _merge_continuation(existing, continuation)
    assert merged.startswith("<html>AAA")
    assert merged.rstrip().endswith("BBB</html>") or "BBB</html>" in merged


def test_merge_continuation_empty_continuation_keeps_existing():
    existing = "<html>done</html>"
    assert _merge_continuation(existing, "   ") == existing


# ---------- 标题回退 ----------


def test_fallback_title_uses_first_chars():
    assert _fallback_title("做一个 复杂的 在线俄罗斯方块 游戏") == "做一个 复杂的 在线俄罗斯方块 游戏"[:20]


def test_fallback_title_collapses_whitespace():
    title = _fallback_title("a\n\nb\t\tc")
    assert title == "a b c"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
