"""登出与会话恢复（US2 / FR-021~FR-027）。

登出的语义边界容易被写错，这里逐条钉死：
① 返回的 anon_key 必须是**新**身份，不是回显旧值；
② Set-Cookie 必须同步下发新值（双通道之一，防网关吞 header）；
③ 携新身份请求**看不到**旧身份的项目（否则等于没登出）；
④ 连续登出各自可用（幂等），每次都是有效签名的新身份。

为什么登出必须走服务端：匿名 cookie 是 HttpOnly 的，前端 JS 原理上清不掉，
只清 localStorage 会让下一次请求又带回旧身份——这正是本条实现要修的缺口。

**写这些用例时的坑**：cookie 优先级高于 ``X-Atoms-Anon`` 请求头，而 ``set_cookie``
会把身份写进 client 的 cookie jar。于是「带 header 请求」在 jar 非空时实际用的是
jar 里的身份，断言会以极其误导的方式失败（表现为「隔离失效」，其实是测试写法
问题）。凡是想按 header 指定身份的地方，都必须先清空 jar——见 ``_as``。
"""

from __future__ import annotations

from dependencies.owner import ANON_COOKIE, ANON_HEADER, _issue, _verify

LOGOUT = "/api/v1/atoms/session/logout"


def _cookies_with(response, name: str) -> list[str]:
    """响应里所有名为 name 的 Set-Cookie 原始串（同名可能有多条）。"""
    return [
        value
        for key, value in response.headers.multi_items()
        if key.lower() == "set-cookie" and value.startswith(f"{name}=")
    ]


async def _as(client, key: str, method: str, url: str, **kwargs):
    """以请求头指定的身份发一次请求，并先清空 cookie jar。

    必须先清：cookie 优先级高于请求头，不清的话 jar 里残留的身份会顶掉这里
    指定的身份，断言就测的不是想测的东西了。
    """
    client.cookies.clear()
    return await getattr(client, method)(url, headers={ANON_HEADER: key}, **kwargs)


async def test_logout_issues_new_identity_and_sets_cookie(client):
    """登出必须换身份，且新身份经 Set-Cookie 与响应体两条通道一起下发。"""
    # 先用一个既有身份建项目，作为「登出后不该再看到」的东西
    old_key = _issue()
    created = await _as(
        client, old_key, "post", "/api/v1/atoms/projects", json={"title": "登出前创建的项目"}
    )
    pid = created.json()["public_id"]

    client.cookies.clear()
    response = await client.post(LOGOUT, headers={ANON_HEADER: old_key})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"

    new_key = body["anon_key"]
    assert new_key != old_key, "登出返回了旧身份——等于没登出"
    assert _verify(new_key) is not None, "登出必须返回**可用**的新身份"

    cookies = _cookies_with(response, ANON_COOKIE)
    assert cookies, "登出未下发 Set-Cookie——前端清不掉 HttpOnly cookie，只能靠它"
    assert f"{ANON_COOKIE}={new_key}" in cookies[0], (
        f"Set-Cookie 里的值不是新身份：{cookies[0]}"
    )
    assert "Max-Age=0" not in cookies[0], (
        "登出以删除旧 cookie 的方式下发了 Set-Cookie：同名 cookie 按顺序应用，"
        "若删除那条排在写入之后，刚签发的新身份会被立刻抹掉"
    )

    # ③ 新身份看不到旧身份的项目
    listed = await _as(client, new_key, "get", "/api/v1/atoms/projects")
    seen = [p["public_id"] for p in listed.json()["projects"]]
    assert pid not in seen, "登出后仍能看到旧身份的项目"

    # 反向确认：旧身份自己仍能取到它（隔离是「换了身份」，不是「删了数据」）
    still = await _as(client, old_key, "get", f"/api/v1/atoms/projects/{pid}")
    assert still.status_code == 200, "登出不应删除旧身份的数据，只是让浏览器不再持有它"


async def test_logout_is_idempotent_and_each_call_yields_usable_identity(client):
    """连续登出：每次都是全新的、可验签的身份，且互不相同。"""
    client.cookies.clear()
    first = (await client.post(LOGOUT)).json()
    client.cookies.clear()
    second = (await client.post(LOGOUT)).json()

    keys = [first["anon_key"], second["anon_key"]]
    assert len(set(keys)) == 2, f"连续登出返回了同一身份：{keys}"
    assert all(_verify(k) is not None for k in keys)

    # 两个身份各自可用
    for key in keys:
        listed = await _as(client, key, "get", "/api/v1/atoms/projects")
        assert listed.status_code == 200, listed.text

    # 且互相隔离
    a, b = keys
    made = await _as(
        client, a, "post", "/api/v1/atoms/projects", json={"title": "A 的项目"}
    )
    pid = made.json()["public_id"]
    other = await _as(client, b, "get", f"/api/v1/atoms/projects/{pid}")
    assert other.status_code == 404, "登出签发的两个身份互相可见"


async def test_cookie_identity_outranks_header(client):
    """身份优先级：cookie 高于 X-Atoms-Anon 请求头。

    登出换的是 cookie 里的身份，所以这条优先级必须成立——否则前端即使拿到了新
    anon_key，只要旧 cookie 还在且优先，请求仍会落回旧身份。
    """
    cookie_key = _issue()
    header_key = _issue()
    created = await _as(
        client, cookie_key, "post", "/api/v1/atoms/projects", json={"title": "cookie 身份的项目"}
    )
    pid = created.json()["public_id"]

    # 这里刻意**不**清 jar：要的就是 cookie 压过请求头
    client.cookies.set(ANON_COOKIE, cookie_key)
    response = await client.get(
        f"/api/v1/atoms/projects/{pid}", headers={ANON_HEADER: header_key}
    )
    assert response.status_code == 200, "cookie 身份未取得优先——优先级被写反了"
