"""html_extract 纯函数单元测试。

对应 specs/001-atoms-demo/tasks.md T013：三级容错提取、完整性校验与 CSP 注入。
本模块无副作用、无 DB 依赖，可独立运行：

    cd app/backend && python -m pytest tests/test_html_extract.py -v
"""

from __future__ import annotations

from services.html_extract import (
    CSP_META_TAG,
    extract_html,
    inject_csp,
    is_complete_document,
    sanitize_generated_html,
)

FULL_DOC = "<!DOCTYPE html>\n<html>\n<head><title>t</title></head>\n<body><p>hi</p></body>\n</html>"


# ------------------------------------------------------------------ 三级提取


def test_extract_from_markdown_fence():
    raw = "好的，这是页面：\n```html\n" + FULL_DOC + "\n```\n希望对你有帮助。"
    assert extract_html(raw) == FULL_DOC


def test_extract_fence_without_language_tag():
    raw = "```\n" + FULL_DOC + "\n```"
    assert extract_html(raw).startswith("<!DOCTYPE html>")


def test_extract_fence_with_crlf():
    raw = "```html\r\n" + FULL_DOC + "\r\n```"
    assert "<html>" in extract_html(raw)


def test_extract_prefers_doctype_over_prefix_text():
    raw = "这是我的设计说明，很长的一段前言。\n" + FULL_DOC
    assert extract_html(raw) == FULL_DOC


def test_extract_from_html_tag_without_doctype():
    raw = "说明文字\n<html><body><p>x</p></body></html>"
    assert extract_html(raw) == "<html><body><p>x</p></body></html>"


def test_extract_fallback_returns_whole_text():
    raw = "<div>not a document</div>"
    assert extract_html(raw) == raw


def test_extract_empty_fence_falls_through():
    raw = "```\n```\n" + FULL_DOC
    assert extract_html(raw) == FULL_DOC


def test_extract_handles_none_and_blank():
    assert extract_html(None) == ""
    assert extract_html("   ") == ""


# ------------------------------------------------------------------ 完整性校验


def test_is_complete_document_accepts_trailing_whitespace():
    assert is_complete_document(FULL_DOC + "\n  \n")


def test_is_complete_document_rejects_truncated():
    assert not is_complete_document("<!DOCTYPE html><html><body><p>cut off")
    assert not is_complete_document("")
    assert not is_complete_document(None)


# ------------------------------------------------------------------ CSP 注入


def test_inject_csp_into_head_once():
    result = inject_csp(FULL_DOC)
    assert result.count("Content-Security-Policy") == 1
    assert "<head>\n    " + CSP_META_TAG in result


def test_inject_csp_creates_head_when_missing():
    raw = "<!DOCTYPE html>\n<html>\n<body><p>x</p></body>\n</html>"
    result = inject_csp(raw)
    assert "<head>" in result and "Content-Security-Policy" in result


def test_inject_csp_skips_existing_csp():
    raw = FULL_DOC.replace(
        "<title>t</title>",
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><title>t</title>',
    )
    result = inject_csp(raw)
    assert result.count("Content-Security-Policy") == 1


# ------------------------------------------------------------------ 组合入口


def test_sanitize_success_path():
    html, error = sanitize_generated_html("```html\n" + FULL_DOC + "\n```")
    assert error is None
    assert html.startswith("<!DOCTYPE html>")
    assert html.rstrip().endswith("</html>")
    assert "Content-Security-Policy" in html


def test_sanitize_rejects_truncated_output():
    html, error = sanitize_generated_html("<!DOCTYPE html><html><body>半截")
    assert html == ""
    assert error and "不完整" in error


def test_sanitize_rejects_non_html_payload():
    html, error = sanitize_generated_html("抱歉，我无法完成这个请求。")
    assert html == ""
    assert error is not None


def test_sanitize_rejects_empty_input():
    html, error = sanitize_generated_html("")
    assert html == ""
    assert error is not None
