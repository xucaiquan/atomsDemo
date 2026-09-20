"""A1/A2：归属隔离的越权矩阵。

两个独立的匿名会话（A、B），B 对 A 的资源做任何读写都必须 404。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from dependencies.owner import ANON_COOKIE, ANON_HEADER, OwnerContext, _issue
from main import app
from models.projects import Projects
from models.versions import Versions
from routers.atoms import ERROR_STATUS, RouteError, _require_project


@pytest.fixture
def two_identities():
    """两个独立的合法签名标识，作为「A 和 B 两个人」的固定身份。"""
    return {"a": _issue(), "b": _issue()}


def _as(identity: str) -> dict[str, str]:
    return {ANON_HEADER: identity}


def _new_client(**kwargs) -> AsyncClient:
    """另起一个**独立会话**的客户端。

    同一个 AsyncClient 会自动持久化 cookie，连续两次请求属于同一个人；
    要模拟「另一个浏览器」必须换一个 client。cookie 只能挂在构造器上——
    httpx 已弃用逐请求 cookies，那样会刷 DeprecationWarning。

    调用方必须依赖 ``atoms_app`` 夹具，否则 ``get_db`` 没有被指向内存库。
    """
    return AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", **kwargs
    )


async def _make_project(identity: str, title: str = "A 的项目") -> str:
    """用**该身份自己的**客户端创建项目。

    不能借用共享的 client：它一旦收过某个身份的 Set-Cookie，后续请求都会带上
    那个 cookie，而 get_owner 的优先级是 cookie > 请求头，于是「另一个人」会被
    解析成同一个身份，测试就失去了隔离含义（HTTP 层仍是各带各的标识）。
    """
    async with _new_client(headers=_as(identity)) as client:
        response = await client.post("/api/v1/atoms/projects", json={"title": title})
        assert response.status_code == 201, response.text
        return response.json()["public_id"]


async def _list_as(identity: str) -> list[dict]:
    """以指定身份（全新会话，不带 cookie）取项目列表。"""
    async with _new_client(headers=_as(identity)) as client:
        response = await client.get("/api/v1/atoms/projects")
        assert response.status_code == 200, response.text
        return response.json()["projects"]


async def _add_project(db_session, owner_key: str | None, *, is_demo: bool = False) -> str:
    """直接落库摆一个夹具项目（不经过路由）。

    演示项目在真实库里就是 ``owner_key IS NULL, is_demo=true`` 的行
    （回填脚本正是按 ``owner_key IS NULL`` 判定演示项目的）。
    """
    public_id = str(uuid.uuid4())
    db_session.add(
        Projects(
            public_id=public_id,
            title="演示项目" if is_demo else "夹具项目",
            owner_key=owner_key,
            version_count=0,
            latest_status=None,
            is_demo=is_demo,
        )
    )
    await db_session.commit()
    return public_id


async def _add_version(
    db_session,
    project_public_id: str,
    seq: int = 1,
    *,
    status: str = "succeeded",
    html: str = "<html><body>夹具</body></html>",
) -> int:
    """直接落库摆一个版本行（不经过路由）。

    **为什么必须摆真行**：``get_version`` / ``get_version_steps`` /
    ``cancel_generation`` 在「查不到版本行」时都会提前 ``return 404``，
    与归属无关。项目若一个版本都没有，任何身份拿到的都是同一个 404——
    那样的越权测试永远为绿，测不出归属过滤是否生效。先摆行再断言两个身份
    拿到**不同**的结果，断言才有牙。
    """
    db_session.add(
        Versions(
            project_public_id=project_public_id,
            seq=seq,
            prompt="夹具需求",
            html=html,
            status=status,
        )
    )
    await db_session.commit()
    return seq


# ------------------------------------------------------------------ A1/A2 越权矩阵


async def test_b_cannot_see_a_project_in_list(atoms_app, two_identities):
    a_id = await _make_project(two_identities["a"])

    assert [p["public_id"] for p in await _list_as(two_identities["a"])] == [a_id]
    assert await _list_as(two_identities["b"]) == []


async def test_b_gets_404_on_a_project_detail(atoms_app, two_identities):
    a_id = await _make_project(two_identities["a"])
    async with _new_client(headers=_as(two_identities["b"])) as other:
        response = await other.get(f"/api/v1/atoms/projects/{a_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_two_anonymous_sessions_are_isolated(client):
    """两个**独立的**浏览器会话必须互不可见。

    注意：httpx.AsyncClient 会自动持久化 cookie，所以同一个 client 连续发两次请求
    属于同一个会话——这里必须开第二个 client 才是真的「另一个人」。
    """
    a = await client.post("/api/v1/atoms/projects", json={"title": "A"})
    assert a.status_code == 201
    public_id = a.json()["public_id"]

    # 同一会话：看得到（验证 cookie 确实生效）
    same = await client.get("/api/v1/atoms/projects")
    assert [p["public_id"] for p in same.json()["projects"]] == [public_id]

    # 另一个全新会话：看不到
    async with _new_client() as other:
        b_list = await other.get("/api/v1/atoms/projects")
        assert b_list.json()["projects"] == []


async def test_cookie_works_same_as_header(client):
    """Set-Cookie 下发的匿名标识与请求头必须**等价**。

    否则 cookie 被网关吃掉（或前端只拿得到响应体里的 anon_key）的部署下，
    用户会看不到自己的项目。
    """
    a = await client.post("/api/v1/atoms/projects", json={"title": "Cookie 会话"})
    public_id = a.json()["public_id"]
    cookie = client.cookies.get(ANON_COOKIE)
    assert cookie, "响应必须 Set-Cookie 下发匿名标识"

    # 同一个标识走请求头（全新 client，不带任何 cookie）：看得到
    async with _new_client(headers=_as(cookie)) as header_client:
        listing = await header_client.get("/api/v1/atoms/projects")
        assert [p["public_id"] for p in listing.json()["projects"]] == [public_id]

    # 同一个标识走 cookie：也看得到
    async with _new_client(cookies={ANON_COOKIE: cookie}) as cookie_client:
        listing = await cookie_client.get("/api/v1/atoms/projects")
        assert [p["public_id"] for p in listing.json()["projects"]] == [public_id]

    # 换成别人的标识：看不到
    async with _new_client(cookies={ANON_COOKIE: _issue()}) as other:
        other_list = await other.get("/api/v1/atoms/projects")
        assert other_list.json()["projects"] == []


# ------------------------------------------------------------------ 演示项目：可读不可写


async def test_demo_project_is_visible_on_read_path(atoms_app, db_session, two_identities):
    """演示项目对任意身份可见（读路径 200），否则新用户进来看到的是一片空白。"""
    demo_id = await _add_project(db_session, None, is_demo=True)

    assert demo_id in [p["public_id"] for p in await _list_as(two_identities["a"])]
    assert demo_id in [p["public_id"] for p in await _list_as(two_identities["b"])]

    async with _new_client(headers=_as(two_identities["a"])) as client:
        detail = await client.get(f"/api/v1/atoms/projects/{demo_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["public_id"] == demo_id


async def test_require_project_treats_demo_as_read_only(db_session):
    """写路径命中演示项目必须 409 CONFLICT，而不是 404（spec §4 S1.2）。

    演示项目在列表里本来就可见，用 404 会让用户以为数据坏了。
    """
    demo_id = await _add_project(db_session, None, is_demo=True)
    ctx = OwnerContext(owner_key=f"anon:{_issue()}")

    readable = await _require_project(db_session, demo_id, ctx, write=False)
    assert readable.public_id == demo_id

    with pytest.raises(RouteError) as excinfo:
        await _require_project(db_session, demo_id, ctx, write=True)
    assert excinfo.value.code == "CONFLICT"
    assert ERROR_STATUS[excinfo.value.code] == 409
    assert "演示项目" in excinfo.value.message


async def test_require_project_fails_closed_on_foreign_project(db_session):
    """他人的项目在写路径必须 404（不泄露存在性）；自己的项目正常返回。"""
    mine_raw = _issue()
    theirs_raw = _issue()
    mine = await _add_project(db_session, f"anon:{mine_raw}")
    theirs = await _add_project(db_session, f"anon:{theirs_raw}")
    ctx = OwnerContext(owner_key=f"anon:{mine_raw}")

    assert (await _require_project(db_session, mine, ctx, write=True)).public_id == mine
    assert (await _require_project(db_session, mine, ctx, write=False)).public_id == mine

    with pytest.raises(RouteError) as excinfo:
        await _require_project(db_session, theirs, ctx, write=True)
    assert excinfo.value.code == "NOT_FOUND"

    with pytest.raises(RouteError) as read_excinfo:
        await _require_project(db_session, theirs, ctx, write=False)
    assert read_excinfo.value.code == "NOT_FOUND"


async def test_require_project_treats_owned_demo_as_read_only(db_session):
    """``is_demo`` 是只读的**权威**来源，即使这一行恰好也归本人。

    写路径若只检查「``_owned`` 命中就放行」，一行同时 ``owner_key=本人`` 且
    ``is_demo=true`` 的数据（回填脚本之外的写入路径、或历史脏数据都可能造出）
    就会变成可写的——演示项目的数据是所有人共享的展示样本，被某个访客改掉/
    删掉是所有访客都受影响的事故。判定复用已经取回的行，不额外查库。
    """
    raw = _issue()
    demo_id = await _add_project(db_session, f"anon:{raw}", is_demo=True)
    ctx = OwnerContext(owner_key=f"anon:{raw}")

    readable = await _require_project(db_session, demo_id, ctx, write=False)
    assert readable.public_id == demo_id

    with pytest.raises(RouteError) as excinfo:
        await _require_project(db_session, demo_id, ctx, write=True)
    assert excinfo.value.code == "CONFLICT"
    assert ERROR_STATUS[excinfo.value.code] == 409
    assert "演示项目" in excinfo.value.message


# ------------------------------------------------------------------ 写路径越权矩阵
#
# 每一条跨身份断言都必须开自己的 client：同一个 AsyncClient 会自动持久化
# cookie，而 get_owner 的优先级是 cookie > 请求头，于是 ``headers=_as(b)``
# 会被上一步落下的 A 的 cookie 压过去，B 实际以 A 的身份请求——测试会在
# 「正确的实现」上变红，或在错误的实现上静默断言错对象。


async def test_b_cannot_delete_a_project(atoms_app, two_identities):
    """B 删不掉 A 的项目（404 fail-closed），且项目确实还在。"""
    a_id = await _make_project(two_identities["a"])

    async with _new_client(headers=_as(two_identities["b"])) as other:
        response = await other.delete(f"/api/v1/atoms/projects/{a_id}")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"

    # 确认确实没被删掉：A 自己的列表里还在，详情也仍然可读
    assert a_id in [p["public_id"] for p in await _list_as(two_identities["a"])]
    async with _new_client(headers=_as(two_identities["a"])) as mine:
        detail = await mine.get(f"/api/v1/atoms/projects/{a_id}")
    assert detail.status_code == 200, detail.text


async def test_b_cannot_generate_on_a_project(atoms_app, two_identities):
    """B 不能让 A 的项目跑生成（404），且没有留下任何版本行。"""
    a_id = await _make_project(two_identities["a"])

    async with _new_client(headers=_as(two_identities["b"])) as other:
        response = await other.post(
            f"/api/v1/atoms/projects/{a_id}/generate",
            json={"prompt": "劫持"},
        )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"

    async with _new_client(headers=_as(two_identities["a"])) as mine:
        detail = await mine.get(f"/api/v1/atoms/projects/{a_id}")
    assert detail.json()["versions"] == []


async def test_b_cannot_cancel_a_version(atoms_app, db_session, two_identities):
    """B 取消不了 A 的版本：必须是 404，而不是「已是终态」的 409。

    摆的是 A 的**已终态**版本：归属校验缺失时 B 会走到状态检查拿到 409
    「该版本已结束，无需取消」——期望值不同，断言因此有牙。
    """
    a_id = await _make_project(two_identities["a"])
    await _add_version(db_session, a_id, 1, status="succeeded")

    async with _new_client(headers=_as(two_identities["b"])) as other:
        response = await other.post(
            f"/api/v1/atoms/projects/{a_id}/versions/1/cancel"
        )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"

    # A 自己取消同一个版本才是 409（证明 404 来自归属而非「版本不存在」）
    async with _new_client(headers=_as(two_identities["a"])) as mine:
        own = await mine.post(f"/api/v1/atoms/projects/{a_id}/versions/1/cancel")
    assert own.status_code == 409, own.text


async def test_b_cannot_read_a_version_html(atoms_app, db_session, two_identities):
    """唯一返回 html 的接口：A 读得到自己的，B 读不到。"""
    a_id = await _make_project(two_identities["a"])
    await _add_version(db_session, a_id, 1, status="succeeded", html="<html>秘密</html>")

    async with _new_client(headers=_as(two_identities["a"])) as mine:
        own = await mine.get(f"/api/v1/atoms/projects/{a_id}/versions/1")
    assert own.status_code == 200, own.text
    assert own.json()["html"] == "<html>秘密</html>"

    async with _new_client(headers=_as(two_identities["b"])) as other:
        response = await other.get(f"/api/v1/atoms/projects/{a_id}/versions/1")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_b_cannot_read_a_version_steps(atoms_app, db_session, two_identities):
    """轮询接口：A 读得到自己的，B 读不到。"""
    a_id = await _make_project(two_identities["a"])
    await _add_version(db_session, a_id, 1, status="succeeded")

    async with _new_client(headers=_as(two_identities["a"])) as mine:
        own = await mine.get(f"/api/v1/atoms/projects/{a_id}/versions/1/steps")
    assert own.status_code == 200, own.text
    assert own.json()["version_seq"] == 1

    async with _new_client(headers=_as(two_identities["b"])) as other:
        response = await other.get(f"/api/v1/atoms/projects/{a_id}/versions/1/steps")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_request_body_owner_key_cannot_hijack_ownership(atoms_app, two_identities):
    """反作弊：请求体里塞 owner_key 不得改变归属。

    这是本次修复的核心攻击面：旧实现直接把 data.owner_key 当归属键写库，
    任何人都能自称是别人。三个身份/角色各用一个独立会话：攻击者建项目、
    攻击者列表、受害者列表——共用 client 会被 cookie 串成同一个人。
    """
    victim = two_identities["a"]          # 受害者的合法签名标识
    attacker = two_identities["b"]

    async with _new_client(headers=_as(attacker)) as attacker_client:
        # 攻击者用受害者的 owner_key 值当请求体字段，同时带自己的合法标识
        response = await attacker_client.post(
            "/api/v1/atoms/projects",
            json={"title": "伪造归属", "owner_key": f"anon:{victim}"},
        )
        assert response.status_code == 201, response.text
        public_id = response.json()["public_id"]
        attacker_list = await attacker_client.get("/api/v1/atoms/projects")

    async with _new_client(headers=_as(victim)) as victim_client:
        victim_list = await victim_client.get("/api/v1/atoms/projects")

    # 项目归攻击者（发起请求的身份），不归受害者的 owner_key
    assert public_id in [p["public_id"] for p in attacker_list.json()["projects"]]
    # 受害者看不到它
    assert public_id not in [p["public_id"] for p in victim_list.json()["projects"]]


async def test_missing_identity_cannot_reach_demo_via_write(atoms_app, db_session):
    """无身份（cookie 是伪造值）只能读演示项目，写一律 409。

    必须摆一个**真的** ``is_demo=true`` 行：删一个硬编码 UUID 时，「写路径没有
    归属校验」也同样是 404，测试名声称的「碰到演示项目」根本没被测到。
    """
    demo_id = await _add_project(db_session, None, is_demo=True)
    missing_id = "9c1f2a34-7b6d-4e2a-9f01-aa10bb20cc31"

    async with _new_client(cookies={ANON_COOKIE: "forged"}) as client:
        response = await client.delete(f"/api/v1/atoms/projects/{demo_id}")
        assert response.status_code == 409, response.text
        body = response.json()["error"]
        assert body["code"] == "CONFLICT"
        assert "演示项目" in body["message"]

        # 演示项目必须还在
        detail = await client.get(f"/api/v1/atoms/projects/{demo_id}")
        assert detail.status_code == 200, detail.text

        # 不存在的 UUID 仍是 404（fail-closed，不泄露存在性）
        absent = await client.delete(f"/api/v1/atoms/projects/{missing_id}")
        assert absent.status_code == 404, absent.text

