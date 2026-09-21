"""FakeAIHub 的行为契约。"""

from __future__ import annotations

import pytest
from schemas.aihub import ChatMessage, GenTxtRequest
from tests.fakes import FakeAIHub


def _request(user_text: str = "hi") -> GenTxtRequest:
    return GenTxtRequest(
        messages=[ChatMessage(role="user", content=user_text)],
        model="test-model",
        max_tokens=128,
    )


async def test_script_string_returns_content():
    fake = FakeAIHub(["<!DOCTYPE html><html><body></body></html>"])
    response = await fake.gentxt(_request())
    assert response.content.startswith("<!DOCTYPE html")


async def test_script_exception_is_raised():
    boom = RuntimeError("upstream down")
    fake = FakeAIHub([boom])
    with pytest.raises(RuntimeError, match="upstream down"):
        await fake.gentxt(_request())


async def test_script_callable_can_branch_on_request():
    def branch(request: GenTxtRequest) -> str:
        return "A" if "分析" in request.messages[-1].content else "B"

    fake = FakeAIHub([branch, branch])
    assert (await fake.gentxt(_request("分析一下"))).content == "A"
    assert (await fake.gentxt(_request("别的"))).content == "B"


async def test_requests_are_recorded_for_assertion():
    fake = FakeAIHub(["ok"])
    await fake.gentxt(_request("记下我"))
    assert len(fake.requests) == 1
    assert fake.requests[0].messages[-1].content == "记下我"


async def test_empty_string_in_script_simulates_empty_content():
    """脚本里的 ``""`` 即「模型返回空内容」——空内容路径已经可以直接脚本化。

    这条测试的存在使得「耗尽时返回空」不再有必要：模拟空内容不需要借用
    耗尽语义。
    """
    fake = FakeAIHub(["", "<!DOCTYPE html><html><body></body></html>"])
    assert (await fake.gentxt(_request())).content == ""
    assert (await fake.gentxt(_request())).content.startswith("<!DOCTYPE html")


async def test_exhausted_script_fails_loudly():
    """脚本用完后**抛断言**，而不是返回空内容。

    耗尽意味着测试脚本与实际调用次数不符（通常是流水线多调了一次）。若把它
    降级成「空内容」，流水线会走进自己的空内容重试/续写分支，测试最终以一个
    与真实缺陷无关的症状失败——排查成本高得多。响亮的断言把「你脚本写少了」
    直接指出来。

    需要模拟空内容时请用 ``test_empty_string_in_script_simulates_empty_content``
    的写法（脚本里放 ``""``），不要改回本行为。
    """
    fake = FakeAIHub([])
    with pytest.raises(AssertionError, match="脚本已耗尽"):
        await fake.gentxt(_request())
