"""GENERATION_INLINE：请求内同步执行流水线，且复用请求会话。"""

from __future__ import annotations

import asyncio

import pytest

from schemas.aihub import GenTxtRequest
from services import pipeline as pipeline_module
from tests.fakes import FakeAIHub

HTML_OK = "<!DOCTYPE html><html><head></head><body>OK</body></html>"
ANALYSIS_JSON = '{"app_name":"测试应用","features":["记一笔"],"notes":"记账"}'
DESIGN_JSON = (
    '{"layout":"两栏","components":["表单","列表"],"state":["items"],'
    '"interactions":["新增→列表变化"]}'
)

# 请求返回的观察窗口。远大于一次纯内存的受理往返（毫秒级），
# 又远小于「请求内 await 整条流水线」的耗时，足以区分二者。
ACCEPT_WINDOW_SECONDS = 2.0


@pytest.fixture
def inline(monkeypatch):
    monkeypatch.setenv("GENERATION_INLINE", "1")


@pytest.fixture
def fake_ai(monkeypatch):
    """三个阶段的响应：分析 JSON → 设计 JSON → HTML。"""
    fake = FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, HTML_OK])
    monkeypatch.setattr(pipeline_module, "AIHubService", lambda: fake)
    return fake


class _BlockingUpstream:
    """首个模型调用**阻塞在 Event 上**的假上游，让流水线稳定停驻。

    注入点用 ``_PIPELINE_AI_FACTORY``：后台路径在
    ``_run_generation_in_background`` 里通过它取假上游。
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def gentxt(self, request: GenTxtRequest):
        from schemas.aihub import GenTxtResponse

        self.calls += 1
        if self.calls > 1:
            # 释放之后让流水线能干净收尾（阶段 3 拿到完整文档即成功），
            # 避免测试结束时留下半途的任务与未关闭的会话。
            return GenTxtResponse(content=HTML_OK, model=request.model)
        self.entered.set()
        await self.release.wait()
        return GenTxtResponse(content=ANALYSIS_JSON, model=request.model)


async def _spin_until(predicate, seconds: float) -> bool:
    """轮询等待条件成立，返回是否在窗口内成立（不依赖墙上时钟的精度）。"""
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


async def _assert_generate_does_not_await_pipeline(client, inject_fake_ai):
    """断言 ``generate`` 走的是**后台任务**路径，而不是在请求内 await 流水线。

    判别方式是**直接观察机制**：``_start_generation`` 在后台路径里会把任务注册
    进 ``_RUNNING_TASKS``，inline 路径则什么都不注册。上游被阻塞住，任务因此
    一直活着，注册项也就一直可见。

    为什么不用计时判定（「请求是否在 2s 内返回」）：在 ``ASGITransport`` 上不可靠。
    ``asyncio.wait_for`` 超时后取消请求协程，而 ASGI 应用可能吞掉这次取消并正常
    产出 202 响应，``wait_for`` 于是把结果返回——超时被静默吃掉，断言变成空转
    （已实测确认）。

    为什么不用「模型调用数 == 0」：``conftest.py`` 的 ``shared_session_maker`` 已把
    ``db_manager`` 指向测试库，后台任务因此**能**跑通并调用模型，两种模式都会调 3 次。
    """
    import routers.atoms as atoms_module

    upstream = _BlockingUpstream()
    inject_fake_ai(upstream)

    created = await client.post("/api/v1/atoms/projects", json={"title": "T"})
    public_id = created.json()["public_id"]

    request_task = asyncio.create_task(
        client.post(
            f"/api/v1/atoms/projects/{public_id}/generate",
            json={"prompt": "做一个计时器"},
        )
    )

    registered = await _spin_until(
        lambda: any(key[0] == public_id for key in atoms_module._RUNNING_TASKS),
        ACCEPT_WINDOW_SECONDS,
    )
    if not registered:
        request_task.cancel()
        upstream.release.set()
        pytest.fail(
            f"generate 未在 {ACCEPT_WINDOW_SECONDS}s 内注册后台任务——说明它走了 "
            "inline 路径、在请求内 await 流水线，会撞上网关约 120s 的代理读超时"
        )

    # 受理必须已经返回（后台任务跑着，响应不该被它拖住）
    returned = await _spin_until(request_task.done, ACCEPT_WINDOW_SECONDS)
    assert returned, "后台任务已注册，但 generate 的响应仍未返回（受理不是异步的）"
    accepted = request_task.result()
    assert accepted.status_code == 202

    # 流水线确实被启动了，而且此刻仍卡在首个模型调用上
    assert await _spin_until(upstream.entered.is_set, ACCEPT_WINDOW_SECONDS)
    assert upstream.calls == 1
    assert not upstream.release.is_set(), "请求返回时上游不应已被释放"

    # 收尾：释放后台任务并给它跑完的时间（不参与断言）
    upstream.release.set()
    await asyncio.sleep(0.05)


async def test_inline_generation_reaches_succeeded(client, inline, fake_ai):
    """inline 模式下流水线在**请求协程内**跑完，版本经 steps 端点到达 ``succeeded``。

    范围声明（避免误以为本用例覆盖了更多）：这两条断言证明的是「三阶段在请求
    返回前已全部完成、且三次调用都真的发生」。它们**不**证明「流水线复用了请求
    的注入会话」——本仓库的 ``conftest.py`` 用 autouse 的 ``shared_session_maker``
    把 ``db_manager.async_session_maker`` 重绑到了同一个内存库，独立会话照样写得
    进去，所以「版本到达 succeeded」在两种会话策略下都成立。原先的 docstring 以
    「没有 ``DATABASE_URL`` ⇒ 独立会话开不了连接」为前提推出会话复用，该前提与
    conftest 的事实矛盾，结论也就不成立（复核报告 §5 第 16 条）。

    断言用 ``len(fake.requests)``（FakeAIHub 的公开录制列表），与
    ``test_pipeline_recovery.py`` 等既有调用计数断言保持同一约定。
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
    assert len(fake_ai.requests) == 3


@pytest.mark.parametrize("flag_value", ["0", "false", "no", "False", "", "  "])
async def test_falsy_flag_values_do_not_enable_inline(
    client, inject_fake_ai, monkeypatch, flag_value
):
    """``GENERATION_INLINE`` 的非真值必须是「关闭」，而不是「开启」。

    ``os.getenv`` 返回字符串，``"0"``/``"false"``/``"no"`` 全是真值——若直接写
    ``if os.getenv("GENERATION_INLINE")``，开发者在 ``app/.env`` 里写
    ``GENERATION_INLINE=0`` 想关掉这个测试逃生舱，反而会把它打开；而
    ``start_app_v2.sh`` 会把 env 文件里每一行 ``KEY=VALUE`` 都 export 进后端进程，
    所以生产环境同样可被这行配置踩中：``generate`` 会在请求内 await 整条 1~4 分钟
    的流水线，撞上网关约 120s 的代理读超时（计划 Global Constraints 第 6 条）。

    判别方式见 ``_assert_generate_does_not_await_pipeline``（观察后台任务是否被
    注册），不用计时也不用数调用次数。
    """
    monkeypatch.setenv("GENERATION_INLINE", flag_value)
    await _assert_generate_does_not_await_pipeline(client, inject_fake_ai)


async def test_env_unset_keeps_generation_out_of_the_request(client, inject_fake_ai, monkeypatch):
    """未置位 GENERATION_INLINE 时，请求内绝不 await 流水线（网关 120s 约束）。

    这是计划 Global Constraints 第 6 条的回归哨兵：``generate`` 恒有请求会话，
    若把「传入了会话」也当作 inline 依据，生产路径就会在请求内同步等待 1~4 分钟
    的流水线。判别方式见 ``_assert_generate_does_not_await_pipeline``。
    """
    monkeypatch.delenv("GENERATION_INLINE", raising=False)
    await _assert_generate_does_not_await_pipeline(client, inject_fake_ai)
