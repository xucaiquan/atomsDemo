"""超时后重试入口的**数据前提**测试。

前端能做到「刷新后识别上次超时、弹窗询问并用原描述重试」，完全依赖接口把两样
东西带出来：

1. ``error_type`` —— 区分「重试有意义」（timeout / budget_exhausted /
   interrupted）与「重试无用」（auth 等）。缺了它，前端只能对所有失败一视同仁
   地弹窗，会诱导用户反复触发注定失败的请求。
2. ``prompt`` —— 重试必须复用**那一次的描述**。输入框在受理时已清空，刷新后
   更是空的；接口不回传，「立即重试」就只能让用户重打一遍，等于没有这个入口。

两条读取路径都要带齐，缺一就会在某条路径上失效：生成中当场超时走轮询快照
（steps 接口），刷新落地后则先读项目详情里的版本列表。故分别断言。
"""

from __future__ import annotations

import pytest

from fakes import ANALYSIS_JSON, DESIGN_JSON, AuthenticationError, FakeAIHub, make_html

PROMPT = "做一个带收藏和分享按钮的每日歌曲推荐页"


async def _create_project(client) -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": "t"})
    return response.json()["public_id"]


async def _generate(client, pid: str) -> int:
    accepted = await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": PROMPT}
    )
    assert accepted.status_code == 202, accepted.text
    return accepted.json()["version_seq"]


async def _snapshot(client, pid: str, seq: int) -> dict:
    res = await client.get(f"/api/v1/atoms/projects/{pid}/versions/{seq}/steps")
    assert res.status_code == 200, res.text
    return res.json()


def _stage3_timeout_script() -> list:
    """前两阶段正常，代码生成阶段持续超时——复现「阶段三超时」这一真实场景。

    超时是可重试分类，流水线会重试若干次；脚本给足超时项，避免脚本耗尽时
    FakeAIHub 抛出的断言掩盖掉真正要观察的失败分类。
    """
    return [ANALYSIS_JSON, DESIGN_JSON] + [TimeoutError("stage 3 timed out")] * 12


@pytest.fixture
def stage3_timeout(inject_fake_ai):
    inject_fake_ai(FakeAIHub(_stage3_timeout_script()))


async def test_steps_snapshot_carries_error_type_and_prompt(client, stage3_timeout):
    """轮询快照：生成中当场超时时，弹窗据此拿到分类与原描述。"""
    pid = await _create_project(client)
    seq = await _generate(client, pid)

    snapshot = await _snapshot(client, pid, seq)

    assert snapshot["status"] == "failed", "用例前提是这一版确实失败了"
    assert snapshot["error_type"] == "timeout", (
        f"超时失败必须归类为 timeout，否则前端不会弹重试窗，实际 {snapshot['error_type']}"
    )
    assert snapshot["prompt"] == PROMPT, "轮询快照没带回原描述，重试按钮将无描述可用"
    assert snapshot["error"], "失败必须带可读原因"


async def test_project_versions_carry_error_type_and_prompt(client, stage3_timeout):
    """项目详情的版本列表：刷新落地后前端首先读它，缺字段则刷新路径失效。"""
    pid = await _create_project(client)
    seq = await _generate(client, pid)

    detail = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
    version = next(v for v in detail["versions"] if v["seq"] == seq)

    assert version["status"] == "failed"
    assert version["error_type"] == "timeout", (
        "版本列表缺 error_type，刷新后无法判断上次是否超时，弹窗不会出现"
    )
    assert version["prompt"] == PROMPT, "版本列表没带原描述，刷新后无法用原描述重试"


async def test_non_timeout_failure_is_classified_differently(client, inject_fake_ai):
    """非超时失败必须归到**别的**分类，否则「只对可重试失败弹窗」形同虚设。

    这是上面两条的对照组：若实现把所有失败都写成 timeout，上面的断言照样全绿，
    而前端会对鉴权失败之类也弹「要不要重试」——正是需要避免的行为。
    """
    inject_fake_ai(FakeAIHub([AuthenticationError("401 unauthorized")]))
    pid = await _create_project(client)
    seq = await _generate(client, pid)

    snapshot = await _snapshot(client, pid, seq)
    assert snapshot["status"] == "failed"
    assert snapshot["error_type"] == "auth", (
        f"鉴权失败被归类为 {snapshot['error_type']}，前端会错误地提示重试"
    )


async def test_retry_with_same_prompt_creates_new_version_and_keeps_failure(
    client, inject_fake_ai
):
    """重试语义：用原描述重跑产生**新版本**，失败版本仍留在历史里。

    保证弹窗里的「立即重试」不是把失败版本就地改写——失败记录要留痕可查，成功
    结果要作为新版本出现，两者都不能丢。
    """
    inject_fake_ai(FakeAIHub(_stage3_timeout_script()))
    pid = await _create_project(client)
    failed_seq = await _generate(client, pid)
    assert (await _snapshot(client, pid, failed_seq))["status"] == "failed"

    # 重试：上游恢复正常
    inject_fake_ai(FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("重试成功的内容")]))
    new_seq = await _generate(client, pid)
    assert new_seq != failed_seq, "重试必须产生新版本，而不是覆盖失败版本"

    detail = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
    by_seq = {v["seq"]: v for v in detail["versions"]}
    assert by_seq[failed_seq]["status"] == "failed", "失败版本应保留在历史中"
    assert by_seq[new_seq]["status"] == "succeeded"
    assert by_seq[new_seq]["prompt"] == PROMPT, "重试版本记录的应是同一份原描述"
