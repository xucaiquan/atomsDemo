"""生成结果的提取与净化。

对应 specs/001-atoms-demo/research.md R6：LLM 输出格式不稳定，即便提示词明确要求
「只返回 HTML」，实际仍会偶发包裹说明文字或 Markdown 围栏。提取失败等于整个产品
失效，因此采用三级容错提取。

本模块设计为**纯函数、无副作用**，是本项目最值得单元测试的部分。
"""

from __future__ import annotations

import re

# 注入到生成页面的内容安全策略。
# 对应 contracts/streaming-events.md 的渲染约束：阻断外联，防止数据外泄，
# 同时保证生成结果自包含（FR-015）。
CSP_CONTENT = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "img-src data: blob:"
)

CSP_META_TAG = f'<meta http-equiv="Content-Security-Policy" content="{CSP_CONTENT}">'

_FENCE_PATTERN = re.compile(
    r"```(?:html|HTML)?[ \t]*\r?\n(?P<body>.*?)(?:```|\Z)",
    re.DOTALL,
)
_DOCTYPE_PATTERN = re.compile(r"<!DOCTYPE\s+html", re.IGNORECASE)
_HTML_OPEN_PATTERN = re.compile(r"<html[\s>]", re.IGNORECASE)
_HEAD_OPEN_PATTERN = re.compile(r"<head[^>]*>", re.IGNORECASE)
_BODY_OPEN_PATTERN = re.compile(r"<body[\s>]", re.IGNORECASE)
_EXISTING_CSP_PATTERN = re.compile(
    r"<meta[^>]+http-equiv\s*=\s*[\"']?content-security-policy",
    re.IGNORECASE,
)


def extract_html(raw: str | None) -> str:
    """三级容错地从模型原始输出中提取 HTML 文本。

    按序尝试：
    1. 匹配 Markdown 围栏 ```html ... ```
    2. 匹配首个 ``<!DOCTYPE html>`` 或 ``<html`` 至文末
    3. 兜底：整体内容作为 HTML

    Args:
        raw: 模型返回的原始文本，允许为 None 或空串。

    Returns:
        提取出的 HTML 文本（已去除首尾空白）。无法提取时返回空串。
    """
    if not raw:
        return ""

    text = raw.strip()
    if not text:
        return ""

    # 第一级：Markdown 围栏。围栏内容若为空则继续降级，避免「仅围栏无内容」误判成功。
    fence_match = _FENCE_PATTERN.search(text)
    if fence_match:
        fenced = fence_match.group("body").strip()
        if fenced:
            text = fenced

    # 第二级：从首个 <!DOCTYPE html> 或 <html 截取至文末，剥掉模型的前置说明文字。
    doctype_match = _DOCTYPE_PATTERN.search(text)
    if doctype_match:
        return text[doctype_match.start():].strip()

    html_match = _HTML_OPEN_PATTERN.search(text)
    if html_match:
        return text[html_match.start():].strip()

    # 第三级：兜底，把剩余内容整体当作 HTML 返回。
    return text.strip()


def is_complete_document(html: str | None) -> bool:
    """校验提取结果是否为完整文档。

    对应 research.md 风险 3：``max_tokens`` 不足会导致 HTML 截断。必须校验以
    ``</html>`` 结尾，不满足则标记失败，**而非静默展示残缺页面**。
    """
    if not html:
        return False
    return html.rstrip().lower().endswith("</html>")


def looks_well_formed(doc: str | None) -> bool:
    """结构完整性校验（设计文档 S3.4），比「以 </html> 结尾」更严格。

    判定条件：① 以 ``</html>`` 结尾；② ``<!DOCTYPE`` 至多 1 个；
    ③ ``<html`` 恰好 1 个；④ ``<body`` 恰好 1 个。

    用编译好的开标签正则计数，``</html>`` / ``</body>`` 不会被误计入。
    续写拼接若把模型「重开的整篇文档」接在原稿后面，会出现两份结构——
    此时末尾恰好也是 ``</html>``，仅靠 is_complete_document 会漏判成脏数据。
    """
    if not is_complete_document(doc):
        return False
    text = doc or ""
    if len(_DOCTYPE_PATTERN.findall(text)) > 1:
        return False
    if len(_HTML_OPEN_PATTERN.findall(text)) != 1:
        return False
    if len(_BODY_OPEN_PATTERN.findall(text)) != 1:
        return False
    return True


def has_duplicate_structure(doc: str | None) -> bool:
    """文档标记是否重复（模型续写时重开了整篇文档的信号，S3.4）。"""
    text = doc or ""
    return (
        len(_DOCTYPE_PATTERN.findall(text)) > 1
        or len(_HTML_OPEN_PATTERN.findall(text)) > 1
        or len(_BODY_OPEN_PATTERN.findall(text)) > 1
    )


# ---------------------------------------------------------------- 增量保留校验
#
# 为什么需要它（2026-09-21 实测结论）：
# 增量迭代时模型会「顺手精简」上一版——实测「每日歌曲推荐 + 不感兴趣按钮」两轮，
# v2 丢掉了 v1 的 ♡ 收藏 / ← 回到今日推荐，还把 🎲 换一首 改名成 🔄 换一批。
# 在阶段三提示词里加硬性保留约束只能减轻、不能根治（加约束后改名消失，但仍有
# 控件丢失）。因此保障必须落在**产物**上：用确定性代码从 v1 提取可见控件文案，
# 在 v2 产出后校验保留率，缺失则带清单定向重试。校验器本身不依赖模型自觉。
#
# 只提取「可见控件文案」而不做 AST 比对，是刻意的取舍：控件文案是用户能直接
# 感知到的功能入口（按钮/导航/标题），字符串提取确定性高、无改写风险；而让模型
# 输出结构化补丁需要另起一套受限生成协议，失败率更高。

# 承载功能入口的标签：按钮、链接、表单控件、区块标题。
_CONTROL_TAG_PATTERN = re.compile(
    r"<(button|a|h1|h2|h3|summary|legend|label)\b[^>]*>(?P<inner>.*?)</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
# value 承载文案的 input（按钮型）。
_INPUT_VALUE_PATTERN = re.compile(
    r"<input\b[^>]*\btype\s*=\s*[\"']?(?:button|submit|reset)[\"']?[^>]*"
    r"\bvalue\s*=\s*[\"'](?P<value>[^\"']+)[\"']",
    re.IGNORECASE,
)
_TAG_STRIP_PATTERN = re.compile(r"<[^>]*>")
# 模板占位符（``${...}`` / ``{{...}}``）内是运行期数据，不是固定文案。
_TEMPLATE_SLOT_PATTERN = re.compile(r"\$\{[^}]*\}|\{\{[^}]*\}\}")
_WHITESPACE_PATTERN = re.compile(r"\s+")

# 文案长度闸门：过短（单个标点）无区分度，过长的多半是整段说明而非控件。
_LABEL_MIN_CHARS = 2
_LABEL_MAX_CHARS = 40


def _normalize_label(raw: str) -> str:
    """把标签内部文本归一化为可比对的控件文案。"""
    text = _TEMPLATE_SLOT_PATTERN.sub(" ", raw)
    text = _TAG_STRIP_PATTERN.sub(" ", text)
    text = text.replace("&nbsp;", " ")
    return _WHITESPACE_PATTERN.sub(" ", text).strip()


def extract_control_labels(html: str | None) -> set[str]:
    """提取页面中可见的控件文案集合（按钮 / 链接 / 区块标题等）。

    作用于**原始 HTML 文本**，因此由 JS 模板字符串渲染的卡片内按钮同样能被
    提取到（它们在源码里依然是 ``<button>...</button>`` 字面量）。模板占位符
    会被剔除，避免把运行期数据当成固定文案。

    Returns:
        归一化后的文案集合；无可提取内容时返回空集合。
    """
    if not html:
        return set()

    labels: set[str] = set()
    for match in _CONTROL_TAG_PATTERN.finditer(html):
        label = _normalize_label(match.group("inner"))
        if _LABEL_MIN_CHARS <= len(label) <= _LABEL_MAX_CHARS:
            labels.add(label)
    for match in _INPUT_VALUE_PATTERN.finditer(html):
        label = _normalize_label(match.group("value"))
        if _LABEL_MIN_CHARS <= len(label) <= _LABEL_MAX_CHARS:
            labels.add(label)
    return labels


def find_missing_controls(
    previous_html: str | None, new_html: str | None
) -> list[str]:
    """找出上一版有、新版却不见了的控件文案。

    判定方式是「文案是否还出现在新版全文中」而非集合差集：模型可能把按钮换了
    标签（``<button>`` 改成 ``<a>``）或调整了嵌套层级，那不算功能丢失，不应误报。
    只有文案**整体消失**才算真正丢了功能入口——这同时覆盖了「改名」
    （旧名消失即判定缺失）。

    Returns:
        缺失文案列表，按其在上一版中的出现顺序排列；无缺失时为空列表。
    """
    previous_labels = extract_control_labels(previous_html)
    if not previous_labels or not new_html:
        return []

    missing = [label for label in previous_labels if label not in new_html]
    # 结果需稳定可复现（要写进提示词与失败记录），按上一版出现位置排序。
    source = previous_html or ""
    missing.sort(key=lambda label: source.find(label))
    return missing


def inject_csp(html: str | None) -> str:
    """向 HTML 文档注入 CSP meta 标签。

    已存在 CSP 时不重复注入；无 ``<head>`` 时退化为在文档最前面补一个最小 head。
    """
    if not html:
        return ""

    text = html.strip()
    if _EXISTING_CSP_PATTERN.search(text):
        return text

    head_match = _HEAD_OPEN_PATTERN.search(text)
    if head_match:
        insert_at = head_match.end()
        return text[:insert_at] + "\n    " + CSP_META_TAG + text[insert_at:]

    html_match = _HTML_OPEN_PATTERN.search(text)
    if html_match:
        close_bracket = text.find(">", html_match.start())
        if close_bracket != -1:
            insert_at = close_bracket + 1
            return (
                text[:insert_at]
                + f"\n<head>\n    {CSP_META_TAG}\n</head>"
                + text[insert_at:]
            )

    return f"<head>\n    {CSP_META_TAG}\n</head>\n" + text


def sanitize_generated_html(raw: str | None) -> tuple[str, str | None]:
    """提取 + 校验 + 注入 CSP 的组合入口。

    Returns:
        ``(html, error)``。成功时 ``error`` 为 None；失败时 ``html`` 为空串，
        ``error`` 为**面向用户的可读中文措辞**（会被前端直接展示）。
    """
    extracted = extract_html(raw)
    if not extracted:
        return "", "模型没有返回可用的页面内容，请调整描述后重试"

    lowered = extracted.lower()
    if "<html" not in lowered and "<body" not in lowered:
        return "", "生成内容无法解析为有效页面，请调整描述后重试"

    if not is_complete_document(extracted):
        return "", "生成的页面内容不完整（可能被截断），请简化需求后重试"

    return inject_csp(extracted), None
