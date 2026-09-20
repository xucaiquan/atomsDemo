"""回滚语义与基线测试（设计文档 S5，验收 A5）。

「回滚即新版本」：restore 创建 max(seq)+1 的新版本逐字节复制目标 html/prompt，
原版本全部保留；下一轮增量自动以回滚结果为基线（不变量 2）。
"""

from __future__ import annotations

import hashlib

from fakes import ANALYSIS_JSON, DESIGN_JSON, FakeAIHub, make_html
from core.database import db_manager
from models.versions import Versions

HTML_V1 = make_html("计算器v1")
HTML_V2 = make_html("计算器v2")


async def _create_project(client) -> str:
    response = await client.post("/api/v1/atoms/projects", json={"title": "t"})
    return response.json()["public_id"]


async def _generate(client, pid: str, prompt: str, html: str) -> int:
    response = await client.post(
        f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": prompt}
    )
    return response.json()["version_seq"]


async def _seed(client, pid: str) -> None:
    # 直接落两个成功版本，避免依赖生成路径（生成路径由其他测试覆盖）
    async with db_manager.session() as session:
        session.add(
            Versions(
                project_public_id=pid, seq=1, prompt="第一版",
                html=HTML_V1, status="succeeded", duration_ms=10,
            )
        )
        session.add(
            Versions(
                project_public_id=pid, seq=2, prompt="第二版",
                html=HTML_V2, status="succeeded", duration_ms=10,
            )
        )
        await session.commit()


async def test_restore_creates_new_version_byte_identical(client, inject_fake_ai):
    pid = await _create_project(client)
    await _seed(client, pid)

    response = await client.post(f"/api/v1/atoms/projects/{pid}/versions/1/restore")
    assert response.status_code == 200
    body = response.json()
    assert body["restored"] is True
    assert body["version_seq"] == 3
    assert body["restored_from"] == 1

    restored = (
        await client.get(f"/api/v1/atoms/projects/{pid}/versions/3")
    ).json()
    # 注入 CSP 后存储的 HTML 原样复制（逐字节一致）
    assert restored["html"] == HTML_V1
    assert restored["prompt"] == "第一版"
    assert restored["status"] == "succeeded"
    assert restored["summary"]["restored_from"] == 1

    # 原版本全部保留，可再次回滚
    v1 = (await client.get(f"/api/v1/atoms/projects/{pid}/versions/1")).json()
    assert v1["html"] == HTML_V1
    again = await client.post(f"/api/v1/atoms/projects/{pid}/versions/2/restore")
    assert again.json()["version_seq"] == 4

    project = (await client.get(f"/api/v1/atoms/projects/{pid}")).json()
    assert project["version_count"] == 4
    assert project["latest_status"] == "succeeded"


async def test_next_generation_baseline_is_restored_version(client, inject_fake_ai):
    """回滚后的下一轮增量，previous_html 基线自动是回滚结果（不变量 2）。"""
    pid = await _create_project(client)
    await _seed(client, pid)
    await client.post(f"/api/v1/atoms/projects/{pid}/versions/1/restore")

    fake = inject_fake_ai(
        FakeAIHub([ANALYSIS_JSON, DESIGN_JSON, make_html("v4")])
    )
    seq = (
        await client.post(
            f"/api/v1/atoms/projects/{pid}/generate",
            json={"prompt": "在回滚后的版本上加深色模式"},
        )
    ).json()["version_seq"]
    assert seq == 4

    code_msg = fake.code_stage_messages()[0]
    assert "计算器v1" in code_msg  # 基线 = 回滚复制的 v1 内容
    assert "计算器v2" not in code_msg  # v2 不再是基线


async def test_restore_rejects_failed_and_missing(client):
    pid = await _create_project(client)
    async with db_manager.session() as session:
        session.add(
            Versions(
                project_public_id=pid, seq=1, prompt="失败版",
                html=None, status="failed", error="生成失败",
            )
        )
        await session.commit()

    response = await client.post(f"/api/v1/atoms/projects/{pid}/versions/1/restore")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"

    missing = await client.post(f"/api/v1/atoms/projects/{pid}/versions/99/restore")
    assert missing.status_code == 404


async def test_html_sha256_matches_content(client):
    """版本接口的 html_sha256 与内容哈希一致（S5.2 预览/源码同一标识）。"""
    pid = await _create_project(client)
    await _seed(client, pid)
    detail = (
        await client.get(f"/api/v1/atoms/projects/{pid}/versions/1")
    ).json()
    expected = hashlib.sha256(detail["html"].encode("utf-8")).hexdigest()
    assert detail["html_sha256"] == expected
    assert len(expected) == 64
