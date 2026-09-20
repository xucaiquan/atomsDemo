"""GENERATION_INLINE：请求内同步执行流水线，且复用请求会话。"""

from __future__ import annotations

import asyncio

import pytest

from schemas.aihub import GenTxtRequest
from services import pipeline as pipeline_module
from tests.fakes import FakeAIHub

HTML_OK = "<!DOCTYPE html><html><head></head><body>OK</body></html>"


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setenv("GENERATION_INLINE", "1")


@pytest.fixture
def fake_ai(monkeypatch):
    """三个阶段的响应：分析 JSON → 设计 JSON → HTML。"""
    fake = FakeAIHub(
        [
            '{"app_name":"测试应用","features":["记一笔"],"notes":"记账"}',
            '{"layout":"两栏","components":["表单","列表"],"state":["items"],"interactions":["新增→列表变化"]}',
            HTML_OK,
        ]
    )
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)
    return fake


async def test_inline_generation_reaches_succeeded(client, inline, fake_ai):
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个记账工具"},
    )
    assert accepted.status_code == 202
    seq = accepted.json()["version_seq"]

    steps = await client.get(
        f"/api/v1/atoms/projects/{public_id}/versions/{seq}/steps"
    )
    body = steps.json()
    assert body["status"] == "succeeded", body
    assert fake_ai.call_count() == 3


async def test_inline_uses_request_session_not_real_db(client, inline, fake_ai):
    """若 inline 路径去连真实库，本测试会因 DATABASE_URL 缺失而报错。"""
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]
    await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个计时器"},
    )
    detail = await client.get(f"/api/v1/atoms/projects/{public_id}")
    assert len(detail.json()["versions"]) == 1


async def test_env_unset_keeps_generation_out_of_the_request(
    client, fake_ai, monkeypatch
):
    """未置位 GENERATION_INLINE 时，请求内绝不 await 流水线（网关 120s 约束）。

    这是计划 Global Constraints 第 6 条的回归哨兵：``generate`` 恒有请求会话，
    若把「传入了会话」也当作 inline 依据，生产路径就会在请求内同步等待 1~4 分钟
    的流水线。后台任务必然去开独立 DB 会话（测试环境无 ``DATABASE_URL``，开不了），
    所以模型一次都不会被调用——``call_count`` 为 0 即证明生成没有被同步等待。
    """
    monkeypatch.delenv("GENERATION_INLINE", raising=False)
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个计时器"},
    )
    assert accepted.status_code == 202

    await asyncio.sleep(0.05)  # 给后台任务留出跑到失败点的时间
    assert fake_ai.call_count() == 0
