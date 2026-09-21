"""归属隔离与越权矩阵测试（设计文档 S1 / S4，验收 A1/A2）。

核心不变量 1：归属由服务端派生（JWT > cookie > 头 > 新签发），
任何客户端字段不得影响归属；越权一律 404（fail-closed），演示项目写 409。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from core.auth import create_access_token
from core.database import db_manager
from models.projects import Projects
from models.versions import Versions

DEMO_HTML = "<!DOCTYPE html><html><head></head><body>demo</body></html>"


async def _seed_demo_project() -> str:
    public_id = str(uuid.uuid4())
    async with db_manager.session() as session:
        session.add(
            Projects(
                public_id=public_id,
                title="演示项目",
                owner_key=None,
                version_count=1,
                latest_status="succeeded",
                is_demo=True,
            )
        )
        session.add(
            Versions(
                project_public_id=public_id,
                seq=1,
                prompt="演示需求",
                html=DEMO_HTML,
                status="succeeded",
                duration_ms=100,
            )
        )
        await session.commit()
    return public_id


async def _seed_orphan_project() -> str:
    """owner_key 为 NULL 且非演示：迁移未完成时的历史数据，应全员不可见。"""
    public_id = str(uuid.uuid4())
    async with db_manager.session() as session:
        session.add(
            Projects(
                public_id=public_id,
                title="孤儿项目",
                owner_key=None,
                version_count=0,
                latest_status=None,
                is_demo=False,
            )
        )
        await session.commit()
    return public_id


async def _create_project(client) -> str:
    response = await client.post(
        "/api/v1/atoms/projects", json={"title": "我的项目"}
    )
    assert response.status_code == 201
    return response.json()["public_id"]


# ------------------------------------------------------------------ 匿名隔离矩阵


async def test_anon_clients_are_isolated_on_every_route(http):
    """访客 B 对访客 A 的项目在全部 9 条路由上都无法读也无法写。"""
    a, b = http(), http()
    async with a, b:
        pid = await _create_project(a)
        assert pid in [p["public_id"] for p in (await a.get("/api/v1/atoms/projects")).json()["projects"]]

        # 读路径全部 404（fail-closed，不用 403 探测存在性）
        assert (await b.get(f"/api/v1/atoms/projects/{pid}")).status_code == 404
        assert (await b.get(f"/api/v1/atoms/projects/{pid}/versions/1")).status_code == 404
        assert (await b.get(f"/api/v1/atoms/projects/{pid}/versions/1/steps")).status_code == 404
        assert (await b.delete(f"/api/v1/atoms/projects/{pid}")).status_code == 404

        # 写路径全部 404
        gen = await b.post(f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": "x"})
        assert gen.status_code == 404
        assert gen.json()["error"]["code"] == "NOT_FOUND"
        assert (await b.post(f"/api/v1/atoms/projects/{pid}/versions/1/cancel")).status_code == 404
        assert (await b.post(f"/api/v1/atoms/projects/{pid}/versions/1/restore")).status_code == 404

        # B 的列表里不出现 A 的项目
        listed = (await b.get("/api/v1/atoms/projects")).json()["projects"]
        assert pid not in [p["public_id"] for p in listed]


async def test_anon_identity_persists_via_cookie_and_header(http):
    """双通道：cookie 自动保持身份；X-Atoms-Anon 头可跨客户端恢复身份。"""
    a = http()
    async with a:
        first = await a.get("/api/v1/atoms/projects")
        anon_key = first.json()["anon_key"]
        assert anon_key and "." in anon_key
        # 响应体与 Set-Cookie 双通道一致
        assert a.cookies.get("atoms_anon") == anon_key
        pid = await _create_project(a)

    # 新客户端仅凭头（模拟网关吃 cookie 的场景）恢复身份
    c = http(headers={"X-Atoms-Anon": anon_key})
    async with c:
        listed = (await c.get("/api/v1/atoms/projects")).json()["projects"]
        assert pid in [p["public_id"] for p in listed]


async def test_forged_anon_header_is_rejected(http):
    """无有效 HMAC 签名的头被忽略，视为全新匿名身份。"""
    a = http()
    forged = "f" * 32 + "." + "a" * 16  # 形状合法但签名错误
    async with a, http(headers={"X-Atoms-Anon": forged}) as attacker:
        pid = await _create_project(a)
        listed = (await attacker.get("/api/v1/atoms/projects")).json()["projects"]
        assert pid not in [p["public_id"] for p in listed]
        # 伪造值不会被回显为身份；服务端另签了新标识
        fresh = (await attacker.get("/api/v1/atoms/projects")).json()["anon_key"]
        assert fresh != forged


async def test_request_body_cannot_claim_ownership(http):
    """请求体里的 owner_key 字段被完全忽略（pydantic 模型不含该字段）。"""
    a = http()
    async with a:
        response = await a.post(
            "/api/v1/atoms/projects",
            json={"title": "冒名项目", "owner_key": "user:victim"},
        )
        assert response.status_code == 201
        pid = response.json()["public_id"]

    victim = http(headers={"Authorization": f"Bearer {create_access_token({'sub': 'victim'})}"})
    async with victim:
        assert (await victim.get(f"/api/v1/atoms/projects/{pid}")).status_code == 404


# ------------------------------------------------------------------ 登录身份


async def test_jwt_identity_isolated_and_precedent(http):
    """JWT 身份优先于匿名 cookie；不同 sub 互相不可见。"""
    token_a = create_access_token({"sub": "user-aaa"})
    token_b = create_access_token({"sub": "user-bbb"})

    a = http()
    b = http(headers={"Authorization": f"Bearer {token_b}"})
    async with a, b:
        await a.get("/api/v1/atoms/projects")     # 先以匿名身份拿到 cookie
        anon_pid = await _create_project(a)       # 匿名身份下的项目
        # 同一客户端升级为登录身份：JWT 优先于已存在的匿名 cookie
        a.headers["Authorization"] = f"Bearer {token_a}"
        jwt_pid = await _create_project(a)
        listed = [p["public_id"] for p in (await a.get("/api/v1/atoms/projects")).json()["projects"]]
        assert jwt_pid in listed
        assert anon_pid not in listed
        # 不同 sub 互相不可见
        assert (await b.get(f"/api/v1/atoms/projects/{jwt_pid}")).status_code == 404


async def test_invalid_bearer_falls_back_to_anon(http):
    """坏 token 不报错（FR-014 匿名可用），静默回落匿名身份。"""
    client = http(headers={"Authorization": "Bearer not-a-real-token"})
    async with client:
        response = await client.get("/api/v1/atoms/projects")
        assert response.status_code == 200
        assert response.json()["anon_key"]  # 回落到匿名并签发


# ------------------------------------------------------------------ 演示项目只读


async def test_demo_project_readable_but_readonly(http):
    pid = await _seed_demo_project()
    a, b = http(), http()
    async with a, b:
        for client in (a, b):
            listed = (await client.get("/api/v1/atoms/projects")).json()["projects"]
            demo = next(p for p in listed if p["public_id"] == pid)
            assert demo["is_demo"] is True
            detail = await client.get(f"/api/v1/atoms/projects/{pid}")
            assert detail.status_code == 200
            version = await client.get(f"/api/v1/atoms/projects/{pid}/versions/1")
            assert version.status_code == 200
            assert version.json()["html"] == DEMO_HTML

        # 全部写路径 409 CONFLICT
        gen = await a.post(f"/api/v1/atoms/projects/{pid}/generate", json={"prompt": "改一下"})
        assert gen.status_code == 409
        assert gen.json()["error"]["code"] == "CONFLICT"
        assert (await a.delete(f"/api/v1/atoms/projects/{pid}")).status_code == 409
        assert (await a.post(f"/api/v1/atoms/projects/{pid}/versions/1/cancel")).status_code == 409
        assert (await a.post(f"/api/v1/atoms/projects/{pid}/versions/1/restore")).status_code == 409


async def test_orphan_project_invisible_to_all(http):
    """owner_key 为 NULL 且非演示的历史数据：对任何身份不可见（fail-closed）。"""
    pid = await _seed_orphan_project()
    client = http()
    async with client:
        assert (await client.get(f"/api/v1/atoms/projects/{pid}")).status_code == 404
        listed = (await client.get("/api/v1/atoms/projects")).json()["projects"]
        assert pid not in [p["public_id"] for p in listed]
