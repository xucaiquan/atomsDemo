"""阶段三「轻量配图」约束测试。

背景（真实故障）：每周食谱项目 v1 不含图片时阶段三已耗时 198.0s，紧贴 200s
上限；v2 追加「食谱都带上图片」后，因环境禁止外部图片，模型转而为 21 道菜
逐张手绘内联 SVG，输出量翻倍，两次调用均被 200s 砍断，落库 error_type=timeout。

根因不是上游故障，而是「配图代码量随条目数线性增长」。故约束的要点是让配图
开销回到**常数级**：一个通用样式类 + 数据里的 emoji/渐变字段驱动，而不是每条
一段插画。下面的断言围绕这个要点，而非抠具体措辞。
"""

from __future__ import annotations

from services import prompts


def test_code_system_forbids_per_item_svg_illustration():
    """必须明确禁止「为每个条目手绘 SVG」——这正是本次超时的直接成因。"""
    text = prompts.CODE_SYSTEM
    assert "禁止为每个条目手绘内联 SVG" in text, (
        "阶段三提示词未禁止逐条手绘 SVG，带图片需求会重新触发输出量膨胀与超时"
    )


def test_code_system_offers_a_constant_cost_image_alternative():
    """只禁不给替代方案会让模型无所适从，必须同时给出可落地的轻量方案。

    断言三件套：渐变背景、emoji、以及「数据字段驱动」的写法，缺一则方案不完整。
    """
    text = prompts.CODE_SYSTEM
    assert "渐变" in text and "emoji" in text, "未给出渐变+emoji 的轻量配图替代方案"
    assert "gradient" in text and "emoji" in text, "未给出数据字段驱动的具体写法示例"
    assert "常数级别" in text, (
        "未点明配图代码量应与条目数无关——这是该约束要达成的核心性质"
    )


def test_external_image_ban_still_present():
    """轻量方案是在「禁外部图片」前提下的补充，原有沙箱约束不得被削弱。"""
    text = prompts.CODE_SYSTEM
    assert "不得引用任何外部资源" in text
    assert "fetch" in text or "XMLHttpRequest" in text


def test_image_rule_reaches_the_actual_code_stage_prompt():
    """约束必须真正出现在发给模型的阶段三消息里。

    CODE_SYSTEM 是 system 消息，若未来有人改写组装逻辑把它丢掉，上面的断言
    仍会全绿而线上行为回退。这里从流水线实际使用的两个入口做一次端到端确认。
    """
    user = prompts.build_code_user(
        prompt="推荐的食谱都带上图片更好一点",
        analysis="{}",
        design="{}",
        previous_html="<!DOCTYPE html><html><body><p>v1</p></body></html>",
    )
    # 用户消息负责携带增量保留约束，system 负责携带配图约束，两者都不可缺。
    assert "现在请输出完整的 HTML 源码。" in user
    assert prompts.INCREMENT_PRESERVE_RULES in user
    assert "禁止为每个条目手绘内联 SVG" in prompts.CODE_SYSTEM
