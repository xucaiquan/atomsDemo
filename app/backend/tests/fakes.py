"""脚本化上游与故障注入（设计文档 S4）。

FakeAIHub 契约：
- script 元素可为 str（作为 content 返回）、Exception（抛出）、
  Callable[[GenTxtRequest], str | Exception]（按请求分支）；
- **记录每一次 GenTxtRequest** —— 断言注入内容（需求历史、上一版 HTML、
  上下文预算）的唯一可靠来源。

异常类按 *类名* 与 pipeline.classify_upstream_error 匹配（不依赖 openai
包的具体版本），status_code 属性用于状态码分支。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Union

from schemas.aihub import GenTxtRequest, GenTxtResponse


class AuthenticationError(Exception):
    """模拟 openai.AuthenticationError（401）。"""

    status_code = 401


class PermissionDeniedError(Exception):
    """模拟 openai.PermissionDeniedError（403，如余额不足）。"""

    status_code = 403


class RateLimitError(Exception):
    """模拟 openai.RateLimitError（429）。"""

    status_code = 429


class InternalServerError(Exception):
    """模拟 openai.InternalServerError（5xx）。"""

    status_code = 503


ScriptItem = Union[str, BaseException, Callable[[GenTxtRequest], Any]]


class FakeAIHub:
    def __init__(self, script: list[ScriptItem]) -> None:
        self._script = list(script)
        self.requests: list[GenTxtRequest] = []

    async def gentxt(self, request: GenTxtRequest) -> GenTxtResponse:
        self.requests.append(request)
        if not self._script:
            raise AssertionError("FakeAIHub 脚本已耗尽，但流水线仍在发起调用")
        item = self._script.pop(0)
        if callable(item):
            item = item(request)
            if inspect.isawaitable(item):
                # 异步回调：在模型调用点执行副作用（如模拟生成中途被取消）
                item = await item
        if isinstance(item, BaseException):
            raise item
        return GenTxtResponse(content=item, model=request.model)

    def user_messages(self) -> list[str]:
        """每次调用的最后一条 user 消息文本（断言注入内容的入口）。"""
        out: list[str] = []
        for req in self.requests:
            users = [m for m in req.messages if m.role == "user"]
            out.append(str(users[-1].content) if users else "")
        return out

    def code_stage_messages(self) -> list[str]:
        """代码生成阶段（要求「输出完整的 HTML 源码」）的用户消息。"""
        return [m for m in self.user_messages() if "现在请输出完整的 HTML 源码。" in m]


# ------------------------------------------------------------------ 脚本常量


ANALYSIS_JSON = '{"app_name":"测试应用","features":["功能一","功能二"],"notes":"测试定位"}'
DESIGN_JSON = '{"layout":"单栏","components":["头部"],"state":[],"interactions":[]}'


def make_html(marker: str) -> str:
    """结构完整、可通过 looks_well_formed 校验的最小页面。"""
    return (
        f"<!DOCTYPE html>\n<html>\n<head><title>{marker}</title></head>\n"
        f"<body><p>{marker}</p></body>\n</html>"
    )
