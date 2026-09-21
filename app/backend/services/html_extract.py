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
