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


# 需求历史回看的最大条数与单条截断长度，避免上下文线性膨胀（research.md R5）。
# 最新一条放宽到 600 字符（设计文档 S3.3）：长需求 + 指代时，短截断会丢掉
# 「不要用 CDN」这类关键约束。
HISTORY_MAX_ITEMS = 6
HISTORY_ITEM_CHARS = 400
HISTORY_LATEST_ITEM_CHARS = 600

# 非成功轮次在历史里的状态标注。必须显式写出「未产出页面」——否则模型会把
# 那一轮当成已经做好的功能，在不存在的产物上「继续叠加」。
HISTORY_STATUS_MARKS = {
    "failed": "（该轮生成失败，未产出页面）",
    "cancelled": "（该轮被用户中止，未产出页面）",
}

# 上下文预算（设计文档 2026-09-20 S3.3）：注入上一版 HTML 与续写历史的上限，
# 防止迭代轮次把 prompt 撑爆导致截断或超时。
PREVIOUS_HTML_MAX_CHARS = 24_000
CONTINUE_HISTORY_MAX_CHARS = 12_000
# 上一版 HTML 超限时保留的尾部长度（页面主体逻辑多在末尾）。
_PREVIOUS_HTML_TAIL_CHARS = 2_000


def build_history_block(history_prompts: list | None) -> str:
    """把本项目此前各版本的需求拼成「需求历史」上下文块。

    用于让模型理解「继续刚刚的需求」「按之前说的」「再优化一下」这类
    引用上文的指令——否则模型只看到孤立的当前短句，无法还原真实意图。
    「最新一条」= 切片最后一个元素，即时间上最靠后、当轮指代最可能指向的需求。

    条目可为 ``str``（视为成功轮次）或 ``(prompt, status)`` 二元组。
    **失败/取消轮次必须进入历史并带状态标注**：这是「第一轮贪吃蛇失败 →
    第二轮说『重新执行』」这一真实场景的唯一依据。若把失败轮次整条剔除，
    历史为空，模型只看到孤立的「重新执行」，会把它当成一个全新需求去理解
    （实测被理解成「任务重做清单」）。标注状态则既保住了指代对象，又明确
    告诉模型那一轮没有产物、应当重做而非在其上叠加。

    注意：**增量基线（previous_html）仍只取成功版本**（FR-002），失败轮次
    只进入「需求历史」这一文本上下文，不会成为改写基线。
    """
    items: list[tuple[str, str]] = []
    for entry in history_prompts or []:
        if isinstance(entry, (tuple, list)):
            text, status = (entry + ("succeeded",))[:2] if isinstance(entry, tuple) else (entry[0], entry[1])
        else:
            text, status = entry, "succeeded"
        text = (text or "").strip()
        if text:
            items.append((text, status or "succeeded"))
    if not items:
        return ""
    recent = items[-HISTORY_MAX_ITEMS:]

    lines = []
    for i, (text, status) in enumerate(recent, start=1):
        limit = HISTORY_LATEST_ITEM_CHARS if i == len(recent) else HISTORY_ITEM_CHARS
        mark = HISTORY_STATUS_MARKS.get(status, "")
        lines.append(f"{i}. {text[:limit]}{mark}")
    return (
        "【本项目此前的需求历史（按时间先后，越靠后越新）】\n"
        + "\n".join(lines)
        + "\n\n"
    )


def truncate_previous_html(html: str | None) -> str:
    """上一版 HTML 注入提示词前的预算闸门（S3.3）。

    未超限原样返回；超限则保留 ``<head>`` 段 + 前部内容 + 末尾 2000 字符，
    中间插入省略标记，让模型仍能看到页面骨架与最新改动。
    """
    if not html:
        return ""
    if len(html) <= PREVIOUS_HTML_MAX_CHARS:
        return html

    lower = html.lower()
    head_end = lower.find("</head>")
    if 0 <= head_end < 6_000:
        head = html[: head_end + len("</head>")]
    else:
        head = html[:4_000]
    tail = html[-_PREVIOUS_HTML_TAIL_CHARS:]
    keep = max(0, PREVIOUS_HTML_MAX_CHARS - len(head) - len(tail) - 64)
    body = html[len(head): len(head) + keep]
    omitted = max(0, len(html) - len(head) - len(body) - len(tail))
    return head + body + f"\n<!-- …已省略 {omitted} 字符… -->\n" + tail


def truncate_continue_history(doc: str) -> str:
    """续写调用 history 中回传已产出文档的预算闸门（S3.3）。

    只回传尾部片段。上限必须大于 pipeline 侧的中断点片段长度
    （CONTINUE_TAIL_CHARS=2000），否则续写点上下文会被裁掉。
    """
    if not doc:
        return ""
    if len(doc) <= CONTINUE_HISTORY_MAX_CHARS:
        return doc
    return "以下是已输出内容的尾部片段：\n" + doc[-CONTINUE_HISTORY_MAX_CHARS:]


ANAPHORA_HINT = (
    "重要：如果本次要求引用了上下文（如「继续」「刚才」「上面」「之前说的」"
    "「按原来的」「重新执行」「再试一次」「再优化一下」等指代），请结合上面的需求历史"
    "理解它的真实含义，把它当作对既有应用的延续、重做或改进，而不是一个孤立、"
    "无法执行的新需求。\n"
    "特别注意：历史中标注「该轮生成失败，未产出页面」或「该轮被用户中止，未产出页面」的条目，"
    "表示那一轮**没有留下任何可用产物**。若本次要求是「重新执行」「再试一次」这类指代，"
    "请按那一轮的原始需求**从头完整实现**，不要把它理解成一个关于「重做/重试」本身的新应用。\n\n"
)


def build_analyze_user(
    prompt: str,
    previous_html: str | None = None,
    history_prompts: list[str] | None = None,
) -> str:
    """构造阶段 1 的用户消息（含需求历史，支持指代消解）。"""
    history = build_history_block(history_prompts)
    if previous_html or history:
        return (
            history
            + ANAPHORA_HINT
            + "这是一个**已有应用的迭代需求**。请基于「保留已有功能 + 叠加新要求」的原则做需求分析。\n\n"
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


def build_design_user(
    prompt: str,
    analysis: str,
    previous_html: str | None = None,
    history_prompts: list[str] | None = None,
) -> str:
    """构造阶段 2 的用户消息（含需求历史）。"""
    base = build_history_block(history_prompts) + f"用户需求：\n{prompt}\n\n阶段一的需求分析结果：\n{analysis}"
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


INCREMENT_PRESERVE_RULES = (
    "【增量迭代的硬性保留约束】\n"
    "上一版是用户**已经看到并认可**的产物。用户本次只提出了一项新增要求，"
    "这不构成任何重做、改名或精简其余部分的授权。违反以下任意一条都视为本次生成失败：\n"
    "1. 上一版存在的每一个按钮、菜单项、输入框、区域与交互，在新版本中都必须**原样保留**，"
    "且**按钮与标题的可见文案必须逐字不变**——例如上一版是「换一首」就不能改成「换一批」，"
    "是「今日流程说明」就不能简写成「流程说明」。改写文案等同于删除该功能。\n"
    "2. 不得以「优化」「精简」「统一风格」「合并重复」为由删除、合并或替换上一版的任何既有功能，"
    "即使你认为某个功能多余、命名不佳或可以做得更好。\n"
    "3. 上一版的预置示例数据（如歌曲、菜谱、条目列表）必须保留原有条目；可以新增，但不得替换或减少。\n"
    "4. 整体布局、配色与视觉风格保持与上一版一致，新增元素要融入现有风格，不要重新设计页面。\n"
    "5. 正确的做法是：完整复制上一版源码，仅在必要位置插入本次新增的内容，其余部分原封不动地输出。\n"
    "输出前请逐一核对：上一版的每个控件与文案是否都还在新版本里、且文字完全一致。"
)


def build_code_user(
    prompt: str,
    analysis: str,
    design: str,
    previous_html: str | None = None,
    history_prompts: list[str] | None = None,
) -> str:
    """构造阶段 3 的用户消息。

    多轮迭代时回传**需求历史 + 上一版 HTML + 本次新指令**：需求历史用于
    消解「继续刚刚的需求」这类指代，上一版 HTML 用于增量改进；不回传逐条
    完整对话，避免上下文随轮次线性膨胀（research.md R5）。
    """
    parts = [
        build_history_block(history_prompts) + ANAPHORA_HINT + f"用户需求：\n{prompt}",
        f"需求分析：\n{analysis}",
        f"结构设计：\n{design}",
    ]
    if previous_html:
        parts.append(
            "以下是**上一版页面的完整源码**。本次是增量迭代，请把它当作**基线**，"
            "在其之上做**最小必要修改**，然后叠加本次的新要求：\n\n"
            + truncate_previous_html(previous_html)
        )
        parts.append(INCREMENT_PRESERVE_RULES)
    parts.append("现在请输出完整的 HTML 源码。")
    return "\n\n".join(parts)
