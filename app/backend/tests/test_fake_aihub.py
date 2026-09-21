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


async def test_exhausted_script_returns_empty_not_error():
    """脚本用完后返回空内容（而不是抛异常），模拟「模型返回空」。"""
    fake = FakeAIHub([])
    assert (await fake.gentxt(_request())).content == ""
