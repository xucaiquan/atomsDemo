"""真实后台任务的取消与「取消后迟到写入」的守卫（FR-032，S3.4）。

本文件是**唯一**运行在非 ``GENERATION_INLINE`` 模式的测试。原因：取消的对象是
``_RUNNING_TASKS`` 里注册的 asyncio 任务，而 INLINE 模式下根本没有任务可取消——
流水线在请求协程内跑完，``task.cancel()`` 这条路结构性不可达。conftest 在导入期
用 ``setdefault`` 打开 INLINE 逃生舱，这里用 ``monkeypatch.delenv`` 在**单个用例
内**关掉它，其它用例不受影响。

三个用例分别锁定取消机制的三件事：

1. ``task.cancel()`` 真的能中断停在模型调用点上的后台任务，且版本/步骤/项目
   三者一致落到 ``cancelled``；
2. 取消之后到达的**失败**不得覆盖取消态（``_fail`` 的 ACTIVE_STATUSES 守卫）；
3. 取消之后到达的**成功**不得覆盖取消态（``_run_stages`` 的阶段边界检查）。

2 与 3 是本文件里唯一能确定性复现「取消与已在路上的模型响应竞争」的方式：
不靠时序碰运气，而是直接让「迟到的那一方」发生。
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from core.database import db_manager
from fakes import ANALYSIS_JSON, DESIGN_JSON, FakeAIHub, make_html
from models.projects import Projects
from models.versions import Versions
from routers import atoms as atoms_module
from services.pipeline import GenerationPipeline

PROMPT_TEXT = "做一个记录每日饮水的计数器"


async def _make_project(client, title: str = "取消") -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": title})
    assert response.status_code in (200, 201), response.text
    return response.json()["public_id"]


async def _generate(client, pid: str):
    return await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": PROMPT_TEXT}
    )


async def _detail(client, pid: str, seq: int = 1) -> dict:
    return (
        await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}")
    ).json()


async def _wait_done(task, timeout: float = 5.0) -> bool:
    """等任务结束（轮询而非 wait_for：超时不该反过来取消被测任务）。"""
    for _ in range(int(timeout / 0.01)):
        if task.done():
            return True
        await asyncio.sleep(0.01)
    return task.done()


async def _parked_script(entered: asyncio.Event, release: asyncio.Event, *, stubborn: bool):
    """返回一个停在模型调用点上的脚本项。

    ``stubborn=True`` 时**吞掉取消并照常返回**——模拟「取消打在了一个已经越过
    await 点的请求上，上游响应仍在路上」这一竞态的另一半。
    """

    async def _call(_request):
        entered.set()
        if stubborn:
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass  # 上游响应已在路上：它不理会取消，照常返回
        else:
            await release.wait()
        return ANALYSIS_JSON

    return _call


async def _project_row(pid: str):
    async with db_manager.session() as session:
        return (
            await session.execute(select(Projects).where(Projects.public_id == pid))
        ).scalars().first()


async def _version_count(pid: str) -> int:
    async with db_manager.session() as session:
        rows = (
            await session.execute(
                select(Versions).where(Versions.project_public_id == pid)
            )
        ).scalars().all()
    return len(rows)


async def test_cancel_interrupts_registered_background_task(
    http, inject_fake_ai, monkeypatch
):
    """FR-032：取消必须中断**真实注册的后台任务**，并把三方状态一起落库。"""
    monkeypatch.delenv("GENERATION_INLINE", raising=False)

    entered, release = asyncio.Event(), asyncio.Event()
    fake = inject_fake_ai(
        FakeAIHub(
            [
                await _parked_script(entered, release, stubborn=False),
                DESIGN_JSON,
                make_html("取消"),
            ]
        )
    )

    client = http()
    async with client:
        pid = await _make_project(client)
        accepted = await _generate(client, pid)
        # 受理接口立即返回：此时后台任务连第一步都还没跑完——这正是绕开
        # 网关 120s 代理读超时的形态（INLINE 模式下这条断言不可能成立）。
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["status"] == "accepted"

        await asyncio.wait_for(entered.wait(), 5)
        snap = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/1/steps")
        ).json()
        assert snap["status"] == "running"
        assert snap["steps"][0]["status"] == "running", "任务未停在阶段 1 的调用点上"

        task = atoms_module._RUNNING_TASKS.get((pid, 1))
        assert task is not None, "后台任务未注册到 _RUNNING_TASKS，取消将无从下手"
        assert not task.done()

        cancelled = await client.post(
            f"/api/v1/atoms/projects/{pid}/versions/1/cancel"
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"

        # 决定性证据：任务是被取消唤醒中断的，不是自己跑完的——上游闸门
        # 自始至终没有被打开，任务却在闸门打开之前就结束了。
        assert await _wait_done(task), (
            "task.cancel() 没能中断停驻在模型调用点的后台任务"
        )
        assert not release.is_set()
        assert len(fake.requests) == 1, "取消之后仍发起了新的模型调用"

        # 版本：终态 cancelled，且不是「失败」——失败记录字段保持空，
        # 前端据此区分「被用户停止」与「生成失败」。
        detail = await _detail(client, pid)
        assert detail["status"] == "cancelled"
        assert "取消" in detail["error"]
        assert detail["error_type"] is None
        assert detail["html"] == ""

        # 步骤：全部落为 cancelled。两个写入者各收一半——取消接口把 running 的
        # 阶段 1 落库，后台任务被取消时的收尾把仍处于活跃态（pending）的阶段 2/3
        # 一并落库；两者都写 cancelled，顺序无关（幂等）。关键是**不得出现
        # failed**——那会与版本的 cancelled 终态自相矛盾。
        snap = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/1/steps")
        ).json()
        assert [s["status"] for s in snap["steps"]] == ["cancelled"] * 3

        # 项目：latest_status 同步落为 cancelled，列表页不会显示「生成中」。
        assert (await _project_row(pid)).latest_status == "cancelled"
        assert await _version_count(pid) == 1

        # 已完成的任务必须从任务表摘除，否则会随每次取消累积（内存泄漏）。
        for _ in range(50):
            if (pid, 1) not in atoms_module._RUNNING_TASKS:
                break
            await asyncio.sleep(0.01)
        assert (pid, 1) not in atoms_module._RUNNING_TASKS, (
            "已结束的任务仍留在 _RUNNING_TASKS 中"
        )


async def test_late_failure_cannot_overwrite_cancelled(http, inject_fake_ai, monkeypatch):
    """取消之后到达的失败不得覆盖取消态，也不得往会话里追加失败说明。

    ``_fail`` 是「迟到失败」在生产里的实际代码路径（``_call_step`` 的重试耗尽
    或通用兜底都会走到它）。这里直接以迟到者的身份调用它，确定性复现竞态里
    失败的一侧。
    """
    monkeypatch.delenv("GENERATION_INLINE", raising=False)

    entered, release = asyncio.Event(), asyncio.Event()
    inject_fake_ai(
        FakeAIHub(
            [
                await _parked_script(entered, release, stubborn=False),
                DESIGN_JSON,
                make_html("取消"),
            ]
        )
    )

    client = http()
    async with client:
        pid = await _make_project(client)
        assert (await _generate(client, pid)).status_code == 202
        await asyncio.wait_for(entered.wait(), 5)
        assert (
            await client.post(f"/api/v1/atoms/projects/{pid}/versions/1/cancel")
        ).status_code == 200

        # 迟到者：与取消接口并发到达的一次失败落库
        async with db_manager.session() as session:
            await GenerationPipeline(session)._fail(
                pid, 1, 1, "迟到的失败", error_type="timeout", attempts=2
            )

        detail = await _detail(client, pid)
        assert detail["status"] == "cancelled", "迟到的失败覆盖了取消态"
        assert "取消" in detail["error"]
        assert detail["error_type"] is None, "取消态被写入了失败分类"
        assert (detail["summary"] or {}).get("error_type") is None
        assert (await _project_row(pid)).latest_status == "cancelled"

        # 用户已停止生成，会话里不该冒出一句「生成失败」——那会把「我停的」
        # 说成「它坏了」，并引导用户去重试一个他刚放弃的请求（FR-017）。
        detail = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
        contents = [m["content"] for m in detail["messages"]]
        assert not any("生成失败" in c for c in contents), (
            f"取消后的会话被追加了失败说明：{contents}"
        )
        assert any("停止本次生成" in c for c in contents), (
            f"取消没有留下可读的会话记录：{contents}"
        )


async def test_late_success_after_cancel_is_discarded(
    http, inject_fake_ai, monkeypatch
):
    """取消之后到达的**成功**不得覆盖取消态，产物必须被丢弃。

    这是 rest-api/CLAUDE.md 所述「取消可能撞上已在路上的模型响应」的另一半：
    上游照常返回，流水线必须靠阶段边界的取消检查停下来，而不是把一版 HTML
    落库成 succeeded——那会让用户看到「我明明停了，它却说完成了」。
    """
    monkeypatch.delenv("GENERATION_INLINE", raising=False)

    entered, release = asyncio.Event(), asyncio.Event()
    fake = inject_fake_ai(
        FakeAIHub(
            [
                await _parked_script(entered, release, stubborn=True),
                DESIGN_JSON,
                make_html("不该落库"),
            ]
        )
    )

    client = http()
    async with client:
        pid = await _make_project(client)
        assert (await _generate(client, pid)).status_code == 202
        await asyncio.wait_for(entered.wait(), 5)

        task = atoms_module._RUNNING_TASKS.get((pid, 1))
        assert task is not None
        assert (
            await client.post(f"/api/v1/atoms/projects/{pid}/versions/1/cancel")
        ).status_code == 200

        assert await _wait_done(task), "后台任务既没被取消也没结束——卡在了取消之后"

        # 迟到的成功确实到达过：阶段 1 的调用发生了（否则这个用例没测到东西）。
        assert len(fake.requests) == 1

        detail = await _detail(client, pid)
        assert detail["status"] == "cancelled", "迟到的成功覆盖了取消态"
        assert detail["html"] == "", "取消之后落库了页面产物"
        assert (await _project_row(pid)).latest_status == "cancelled"
        assert await _version_count(pid) == 1

        # 指纹：阶段 1 是 **succeeded**。这条把「迟到响应真的到达并由阶段边界
        # 检查拦下」与「取消直接打断了调用、阶段 1 根本没完成」区分开——后者
        # 阶段 1 会是 cancelled，那样这个用例就测不到迟到写入这一侧。
        snap = (
            await client.get(f"/api/v1/atoms/projects/{pid}/versions/1/steps")
        ).json()
        assert snap["steps"][0]["status"] == "succeeded", (
            "阶段 1 未完成：迟到的成功没有真的到达，用例退化为普通取消"
        )
        assert any(s["status"] == "cancelled" for s in snap["steps"])
        assert all(s["status"] != "running" for s in snap["steps"])
        # 幂等：再点一次取消会被告知「已结束」，而不是重复落库
        again = await client.post(f"/api/v1/atoms/projects/{pid}/versions/1/cancel")
        assert again.status_code == 409
