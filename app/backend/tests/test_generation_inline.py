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
    """版本经 steps 端点走到 ``succeeded``，本身就是「流水线写的是注入会话」的证明。

    本测试环境没有 ``DATABASE_URL``，``db_manager.session()`` 开不了连接，所以若
    流水线没有复用注入的请求会话（而是去开独立会话），三个阶段一步都写不下去，
    版本不可能到达 ``succeeded``、``call_count`` 也不可能为 3——这两个断言合起来
    已经覆盖了「inline 路径复用请求会话而非真实库」这一性质，无需另设只断言
    「版本数为 1」的测试（那只反映 ``prepare()`` 早已落库，与是否复用会话无关）。
    """
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


async def test_explicitly_disabled_flag_does_not_enable_inline(
    client, fake_ai, monkeypatch
):
    """``GENERATION_INLINE=0`` 必须是「关闭」，而不是「开启」。

    ``os.getenv`` 返回字符串，``"0"``/``"false"``/``"no"`` 全为真值——若直接按
    真值判定，开发者在 ``app/.env`` 里写 ``GENERATION_INLINE=0`` 想关掉这个测试
    逃生舱，反而会把它打开；而 ``start_app_v2.sh`` 会把 env 文件里的每一行
    ``KEY=VALUE`` 都 export 进后端进程，所以生产环境同样可被这行配置踩中，
    ``generate`` 就会在请求内 await 整条 1~4 分钟的流水线，撞上网关 120s 代理
    读超时（计划 Global Constraints 第 6 条）。

    断言方式与 ``test_env_unset_keeps_generation_out_of_the_request`` 一致：后台
    任务必然去开独立 DB 会话（测试环境无 ``DATABASE_URL``，开不了），模型一次都
    不会被调用；只有 inline 被真正开启时 ``call_count`` 才会是 3。
    """
    monkeypatch.setenv("GENERATION_INLINE", "0")
    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    accepted = await client.post(
        f"/api/v1/atoms/projects/{public_id}/generate",
        json={"prompt": "做一个计时器"},
    )
    assert accepted.status_code == 202

    await asyncio.sleep(0.05)  # 给后台任务留出跑到失败点的时间
    assert fake_ai.call_count() == 0


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
