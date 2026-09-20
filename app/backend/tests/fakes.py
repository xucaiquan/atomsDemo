"""测试替身。

FakeAIHub 的两个用途：
1. **编排剧本** —— 精确构造「截断→续写成功」「空→空→空」「429 一次后成功」这类序列，
   不需要真实 key、不花配额。
2. **记录请求** —— `.requests` 是断言「实际注入模型的内容」的唯一可靠来源
   （spec §6 A5 要求断言注入的 prompt 文本，而不是「看着像对」）。
"""

from __future__ import annotations

from typing import Any, Callable

from schemas.aihub import GenTxtRequest, GenTxtResponse

ScriptItem = str | Exception | Callable[[GenTxtRequest], "str | Exception"]


class FakeAIHub:
    """脚本化上游模型服务。

    ``script`` 元素依次出队，可以是：
    - ``str``                                   → 作为 ``GenTxtResponse.content`` 返回
    - ``Exception``                             → 抛出
    - ``Callable[[GenTxtRequest], str|Exception]`` → 按请求分支（可复用同一元素多次）
    """

    def __init__(self, script: list[ScriptItem] | None = None) -> None:
        self.script: list[ScriptItem] = list(script or [])
        self.requests: list[GenTxtRequest] = []

    async def gentxt(self, request: GenTxtRequest) -> GenTxtResponse:
        self.requests.append(request)

        item: Any = self.script.pop(0) if self.script else ""
        if callable(item) and not isinstance(item, Exception):
            item = item(request)
        if isinstance(item, Exception):
            raise item

        return GenTxtResponse(
            content=str(item), model=request.model, usage=None
        )

    # ---- 断言辅助 ----

    def user_texts(self) -> list[str]:
        """每次调用中最后一条 user 消息的文本，按调用顺序。"""
        texts: list[str] = []
        for request in self.requests:
            for message in reversed(request.messages):
                if message.role == "user":
                    texts.append(message.content)
                    break
        return texts

    def call_count(self) -> int:
        return len(self.requests)
