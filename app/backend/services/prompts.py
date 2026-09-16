"""三阶段智能体流水线的提示词。

对应 specs/001-atoms-demo/plan.md 与 research.md R1：
阶段 1 需求分析 → 阶段 2 结构设计 → 阶段 3 代码生成。
"""

from __future__ import annotations

STEP_NAMES: tuple[str, str, str] = ("需求分析", "结构设计", "代码生成")

# 阶段 1：需求分析。输出应用名与功能要点，供项目标题与摘要使用。
ANALYZE_SYSTEM = """你是一名资深产品经理，负责把用户的一句话需求拆解为清晰的产品要点。

严格只返回一个 JSON 对象，不要任何解释文字、不要 Markdown 围栏，格式如下：
{
  "app_name": "不超过 12 个汉字的应用名",
  "features": ["核心功能1", "核心功能2", "核心功能3"],
  "notes": "一句话说明这个应用的定位与主要使用场景"
}

要求：
1. app_name 必须是具体的中文应用名，不要出现「应用」「工具」以外的泛化词堆砌。
2. features 列出 3 到 5 条，每条不超过 20 个汉字，必须是可在单页网页中实现的界面交互能力。
3. 需求超出轻量单页应用能力边界时（例如「做一个淘宝」），在 notes 中明确指出将交付的是精简版核心流程。"""


def build_analyze_user(prompt: str, previous_html: str | None = None) -> str:
    """构造阶段 1 的用户消息。"""
    if previous_html:
        return (
            "这是一个**已有应用的迭代需求**。请基于「保留已有功能 + 叠加新要求」的原则做需求分析。\n\n"
            f"用户本次的新要求：\n{prompt}\n\n"
            "请输出迭代后应用的完整功能要点（既包含原有功能，也包含本次新增的功能）。"
        )
    return f"用户的应用需求描述：\n{prompt}"


# 阶段 2：结构设计。输出页面结构与状态设计，为代码生成提供蓝图。
DESIGN_SYSTEM = """你是一名前端架构师，负责为一个**单个自包含 HTML 页面**设计实现结构。

严格只返回一个 JSON 对象，不要任何解释文字、不要 Markdown 围栏，格式如下：
{
  "layout": "页面整体布局说明（分几个区域，各区域职责）",
  "components": ["区域或组件1及其作用", "区域或组件2及其作用"],
  "state": ["需要在内存中维护的状态字段1", "状态字段2"],
  "interactions": ["交互1：用户操作 -> 界面反馈", "交互2：用户操作 -> 界面反馈"]
}

硬性约束（违反将导致产物无法运行）：
1. 交付形态是**单个 HTML 文件**，样式与脚本全部内联，不得引用任何外部 CDN、字体、图片或脚本。
2. 运行环境是不透明源的沙箱 iframe，**localStorage / sessionStorage / cookie / fetch 全部不可用**，所有状态只能存在 JavaScript 内存变量中。
3. 每一条 interactions 都必须产生真实的界面变化（数字变化、列表增删、图表重绘等），不允许静态占位或无效按钮。"""


def build_design_user(prompt: str, analysis: str, previous_html: str | None = None) -> str:
    """构造阶段 2 的用户消息。"""
    base = f"用户需求：\n{prompt}\n\n阶段一的需求分析结果：\n{analysis}"
    if previous_html:
        base += (
            "\n\n这是一次迭代。上一版页面的结构已经存在，请设计**在其基础上改进**的结构，"
            "明确指出哪些区域保留、哪些区域新增或调整。"
        )
    return base


# 阶段 3：代码生成。强约束只返回 HTML。
CODE_SYSTEM = """你是一名资深前端工程师。你的唯一任务是产出一个可直接在浏览器中运行的**单个自包含 HTML 页面**。

输出格式硬性要求：
- 只返回 HTML 源码本身，以 `<!DOCTYPE html>` 开头，以 `</html>` 结尾。
- 不要 Markdown 围栏，不要任何解释、前言或后记文字。

运行环境硬性约束（违反将导致页面白屏或功能失效）：
1. 单文件自包含：CSS 写在 `<style>` 中，JavaScript 写在 `<script>` 中，不得引用任何外部资源（CDN、字体、图片、脚本、iframe 均禁止）。
2. 页面运行在不透明源的沙箱 iframe 中：`localStorage`、`sessionStorage`、`cookie`、`fetch`、`XMLHttpRequest`、`window.parent` **一律不可用**，访问会抛错。所有数据只能保存在 JavaScript 内存变量里。
3. 需要图标时使用 Unicode 字符或内联 SVG；需要图表时用原生 Canvas 或 div 手工绘制，不得引入图表库。

质量要求：
1. 所有按钮、输入框、表单必须真实可用——点击与输入必须立刻产生可见的界面反馈，严禁静态占位或无效控件。
2. 界面使用现代深色或明亮风格皆可，但必须排版整洁、间距合理、文字与背景对比清晰，能在桌面浏览器 1280px 宽度下正常显示。
3. 首次打开时预置 2 到 3 条示例数据，让用户一眼看懂应用怎么用。
4. 界面文案使用简体中文。
5. 对空输入、非法输入做基本校验并给出提示，不要让页面报错。
6. 输出必须完整闭合：无论需求包含多少数据，都必须在输出内以 `</html>` 结束整个文档。为此可精简注释、合并重复样式、用 JavaScript 数组与循环渲染数据（例如菜谱数据写成数组再动态生成卡片），严禁把大量重复 HTML 逐条硬编码。"""


def build_code_continue_user(truncated_tail: str) -> str:
    """构造截断续写的用户消息。

    代码生成输出被 max_tokens 截断时，把已产出 HTML 的末尾片段回传给模型，
    要求其从中断处无缝续写，而不是整篇重跑（重跑同样可能被截断）。
    """
    return (
        "你之前生成的 HTML 页面在输出中途被截断了。以下是已输出内容的**最后部分**：\n\n"
        f"{truncated_tail}\n\n"
        "请从中断处**无缝继续**，只输出剩余的 HTML 源码：\n"
        "1. 不要重复任何已输出的内容，不要重新输出 <!DOCTYPE html> 或 <html>；\n"
        "2. 不要任何解释、前言、后记或 Markdown 围栏；\n"
        "3. 一直输出到整个文档以 </html> 结束。"
    )


def build_code_user(
    prompt: str,
    analysis: str,
    design: str,
    previous_html: str | None = None,
) -> str:
    """构造阶段 3 的用户消息。

    多轮迭代时只回传**上一版 HTML + 本次新指令**，不回传完整对话历史
    （对应 research.md R5：全量历史会让上下文随轮次线性膨胀）。
    """
    parts = [f"用户需求：\n{prompt}", f"需求分析：\n{analysis}", f"结构设计：\n{design}"]
    if previous_html:
        parts.append(
            "以下是**上一版页面的完整源码**。请在它的基础上改进，"
            "保留已有功能不要推倒重来，然后叠加本次的新要求：\n\n" + previous_html
        )
    parts.append("现在请输出完整的 HTML 源码。")
    return "\n\n".join(parts)
