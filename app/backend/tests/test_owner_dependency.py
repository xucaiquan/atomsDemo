"""归属键的签名、验签与三级派生。"""

from __future__ import annotations

import hmac
import logging
from hashlib import sha256

import pytest
from fastapi import Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient

from core.auth import create_access_token
from core.config import settings
from dependencies import owner as owner_module
from dependencies.owner import (
    ANON_COOKIE,
    ANON_HEADER,
    OwnerContext,
    _issue,
    _sign,
    _verify,
    get_owner,
)


# ---------- 纯函数：签名与验签 ----------


def test_issue_produces_verifiable_value():
    raw = _issue()
    assert _verify(raw) is not None
    assert len(raw.split(".")) == 2


def test_issue_is_unique_per_call():
    assert _issue() != _issue()


def test_owner_key_length_within_model_limit():
    """projects.owner_key 的模型上限是 64 字符。"""
    assert len(f"anon:{_issue()}") <= 64


def test_verify_rejects_tampered_nonce():
    raw = _issue()
    nonce, sig = raw.split(".")
    tampered = f"{'0' * len(nonce)}.{sig}"
    assert _verify(tampered) is None


def test_verify_rejects_tampered_signature():
    raw = _issue()
    nonce, sig = raw.split(".")
    flipped = "0" if sig[0] != "0" else "1"
    assert _verify(f"{nonce}.{flipped}{sig[1:]}") is None


def test_verify_rejects_client_made_key():
    """客户端自造 anon-xxx 必须无效——这正是旧 getOwnerKey 的问题。"""
    assert _verify("anon-abcdefgh-1234567890") is None
    assert _verify("") is None
    assert _verify(None) is None


def test_verify_rejects_wrong_number_of_parts():
    assert _verify("no-dot-here") is None
    assert _verify("a.b.c") is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        ".",
        ".sig",
        "nonce.",
        "a" * 64 + "." + "b" * 16,
    ],
)
def test_verify_rejects_hostile_input_without_raising(raw):
    """垃圾输入必须**被拒绝**，而不是抛异常。

    旧实现直接 ``raw.split(".")``：``None`` 会抛 ``AttributeError``。这个异常
    从 ``get_owner`` 里逃逸后不会变成 401/403，而是变成 500——访客只要发一个
    ``Authorization`` 或 cookie 里带 ``None`` 语义的空值就能把任意路由打成 500。
    这也是 T016 要求补的那条「拒绝而非 AttributeError」加固测试。
    """
    try:
        assert _verify(raw) is None
    except AttributeError as exc:  # pragma: no cover - 失败路径
        pytest.fail(f"_verify({raw!r}) 抛了 AttributeError（线上表现为 500）：{exc}")


def test_verify_rejects_non_hex_shape():
    """形状校验不能只看长度：``"z"*32`` 长度合法、且能被 ``_sign`` 算出签名，
    于是 ``hmac.compare_digest`` 会认可它——产出非规范的归属键。"""
    nonce = "z" * 32
    assert _verify(f"{nonce}.{_sign(nonce)}") is None

    good_nonce = _issue().split(".")[0]
    upper = _sign(good_nonce).upper()
    assert _verify(f"{good_nonce}.{upper}") is None, "大写十六进制不是 _sign 的输出形状"


def test_verify_rejects_non_canonical_shape():
    """形状必须与 ``_issue`` 的输出严格一致，否则 ``anon:`` 键会突破长度上限。

    这是**有签名密钥**的攻击面：验签只保证「这个 nonce 是你签过的」，
    不保证 nonce 有多长。攻击者可以发 ``atoms_anon=<超长 nonce>.<有效签名>``，
    得到 ``anon:{超长 nonce}`` 归属键，超过 ``projects.owner_key`` 的 64 字符列限。
    旧实现在写入处用 ``[:64]`` 截断把它盖住了；截断被删掉之后，这条路径就真的
    能打穿写入——所以必须在验签处按形状拒绝。
    """
    long_nonce = "a" * 64
    assert _verify(f"{long_nonce}.{_sign(long_nonce)}") is None

    nonce = _issue().split(".")[0]
    sig = _sign(nonce)
    assert _verify(f"{'z' * 32}.{_sign('z' * 32)}") is None  # 非 hex
    assert _verify(f"{nonce}.{sig}00") is None  # 签名过长
    assert _verify(f"{nonce}.{sig[:8]}") is None  # 签名过短


# ---------- 依赖：三级派生 ----------


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe(owner: OwnerContext = Depends(get_owner)):
        return {"owner_key": owner.owner_key, "anon_key": owner.anon_key}

    return app


async def _call(headers=None, cookies=None) -> dict:
    # cookie 挂在 client 上而非逐请求传：httpx 已弃用 per-request cookies，
    # 逐请求传会为每个用例刷一条 DeprecationWarning。
    async with AsyncClient(
        transport=ASGITransport(app=_probe_app()),
        base_url="http://test",
        cookies=cookies or None,
    ) as http_client:
        response = await http_client.get("/probe", headers=headers or {})
        return response.json()


async def test_anonymous_gets_server_issued_key():
    body = await _call()
    assert body["owner_key"].startswith("anon:")
    assert body["anon_key"] is not None
    assert _verify(body["anon_key"]) is not None


async def test_cookie_is_honoured():
    raw = _issue()
    body = await _call(cookies={ANON_COOKIE: raw})
    assert body["owner_key"] == f"anon:{raw}"
    assert body["anon_key"] == raw


async def test_header_is_honoured():
    raw = _issue()
    body = await _call(headers={ANON_HEADER: raw})
    assert body["owner_key"] == f"anon:{raw}"


async def test_forged_cookie_falls_through_to_new_identity():
    body = await _call(cookies={ANON_COOKIE: "anon-forged-value"})
    assert body["owner_key"] != "anon:anon-forged-value"
    assert body["owner_key"].startswith("anon:")


async def test_login_subject_wins_over_cookie():
    """登录身份必须优先于匿名 cookie，否则登录后看不到自己的项目。"""
    token = create_access_token({"sub": "user-123", "email": "a@b.c"})
    body = await _call(
        headers={"Authorization": f"Bearer {token}"},
        cookies={ANON_COOKIE: _issue()},
    )
    assert body["owner_key"].startswith("user:")
    assert body["anon_key"] is None


async def test_invalid_token_falls_back_to_anonymous():
    body = await _call(headers={"Authorization": "Bearer not-a-jwt"})
    assert body["owner_key"].startswith("anon:")


# ---------- user: 键的长度上限与单射性 ----------


async def _owner_key_for_subject(subject: str) -> str:
    """走真实派生路径（签名 JWT → get_owner），而不是在测试里重算公式。"""
    token = create_access_token({"sub": subject})
    body = await _call(headers={"Authorization": f"Bearer {token}"})
    return body["owner_key"]


async def test_long_subject_yields_key_within_column_limit():
    """users.id 是 String(255)，sub 可远超 59 字符；projects.owner_key 是 String(64)。"""
    subject = "u" * 300
    owner_key = await _owner_key_for_subject(subject)
    assert owner_key.startswith("user:")
    assert len(owner_key) <= 64
    # 原始 sub 不得出现在键里
    assert subject not in owner_key


async def test_distinct_long_subjects_yield_distinct_keys():
    """共享长前缀的不同 sub 必须产出**不同**归属键（数据泄露，而非外观问题）。

    T015 的规格：三个 sub 长度分别为 80 / 60 / 60 且前缀相同。
    其中 60 字符那两个是关键的判别式——``f"user:{sub}"[:64]`` 对它们的截断结果
    完全相同（"user:" + 前 59 字符），三个 sub 会塌缩成一个归属键：A 登录后
    能看到 B 的项目。早期版本用 300 字符前缀构造，虽然也能暴露截断问题，但
    掩盖了一个更现实的情况——**只有 60 字符左右（真实 OIDC sub 的常见长度）**
    就已经碰撞，不需要 300 字符的极端输入。
    """
    prefix = "p" * 59
    subjects = [prefix + "x" * 21, prefix + "b", prefix + "c"]
    assert len(subjects[1]) == 60 and len(subjects[2]) == 60, "前置条件：等长且共享前缀"
    assert len(subjects[0]) == 80

    keys = [await _owner_key_for_subject(s) for s in subjects]
    assert len(set(keys)) == 3, f"三个不同 sub 塌缩了：{keys}"

    # 也覆盖超长 sub（users.id 是 String(255)）
    long_keys = {
        await _owner_key_for_subject("u" * 255 + "a"),
        await _owner_key_for_subject("u" * 255 + "b"),
    }
    assert len(long_keys) == 2

    # 每个键都必须在 projects.owner_key 的 String(64) 上限内
    assert all(len(k) <= 64 for k in keys + list(long_keys))


async def test_same_subject_yields_stable_key():
    """键必须跨请求稳定，否则用户每次访问都看不到自己的项目。"""
    assert await _owner_key_for_subject("stable-user") == await _owner_key_for_subject("stable-user")


# ---------- 密钥未配置时的降级 ----------


def _drop_jwt_secret(monkeypatch) -> None:
    """复现「JWT_SECRET_KEY 未配置」：settings.__getattr__ 只在缺失时读环境变量，
    已缓存的值在 settings.__dict__ 里，删掉它即可回到未配置状态。"""
    monkeypatch.delitem(settings.__dict__, "jwt_secret_key", raising=False)
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)


async def test_missing_secret_still_issues_anonymous_identity(monkeypatch):
    """_issue() 在每条匿名路径上都会跑，密钥缺失时若抛错则所有匿名路由 500。"""
    _drop_jwt_secret(monkeypatch)
    body = await _call()
    assert body["owner_key"].startswith("anon:")
    assert body["anon_key"]


def test_missing_secret_warns_once(monkeypatch, caplog):
    """降级本身是对的，但运维必须能从日志发现匿名身份已可伪造；且不能每请求刷屏。"""
    _drop_jwt_secret(monkeypatch)
    monkeypatch.setattr(owner_module, "_warned_missing_secret", False, raising=False)

    with caplog.at_level(logging.WARNING, logger="dependencies.owner"):
        _sign("nonce-1")
        _sign("nonce-2")
        _sign("nonce-3")

    hits = [r for r in caplog.records if "JWT_SECRET_KEY" in r.getMessage()]
    assert len(hits) == 1


# ---------- cookie 传输属性：Secure 由请求实际 scheme 推导（T018） ----------


def _override_setting(monkeypatch, name: str, value: str | None) -> None:
    """设置/清除被 settings 动态读取的环境变量。

    ``settings.__getattr__`` 只在属性**未被缓存**时读环境变量；读过的值会落在
    ``settings.__dict__`` 里。所以改 env 之后必须连带清掉缓存，否则读到旧值，
    测试会以「改了环境变量却没生效」的方式假绿。

    这里只负责**让覆盖在用例内生效**；用例结束后的回收由 conftest 的
    ``restore_settings_cache`` 按值恢复 ``settings.__dict__`` 兜底。不要依赖
    ``monkeypatch.delitem`` 回滚缓存：键在用例开始时不存在时它**不登记回滚**，
    而用例内的 ``__getattr__`` 会把 env 值写回缓存，该值随后泄漏到所有后续用例
    （``ANON_COOKIE_SECURE='1'`` 就曾因此让 28 个用例失败）。
    """
    monkeypatch.delitem(settings.__dict__, name, raising=False)
    if value is None:
        monkeypatch.delenv(name.upper(), raising=False)
    else:
        monkeypatch.setenv(name.upper(), value)


def _set_cookie_values(response) -> list[str]:
    return [
        v for k, v in response.headers.multi_items()
        if k.lower() == "set-cookie" and v.startswith(f"{ANON_COOKIE}=")
    ]


async def _cookie_for(app, scheme: str, headers: dict | None = None) -> str:
    """在指定 scheme 下取一条匿名 cookie 的 Set-Cookie 原始串（无则返回空串）。"""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"{scheme}://testserver"
    ) as ac:
        response = await ac.get("/api/v1/atoms/projects", headers=headers or {})
    values = _set_cookie_values(response)
    assert values, "匿名入口未下发 cookie，无法判断 Secure 属性"
    return values[0]


async def test_https_request_gets_secure_cookie(app, monkeypatch):
    """HTTPS 下必须带 Secure（设计文档 §S1：HttpOnly; SameSite=Lax; Secure(生产)）。"""
    _override_setting(monkeypatch, "anon_cookie_secure", None)
    raw = await _cookie_for(app, "https")
    assert "secure" in raw.lower(), f"HTTPS 下 cookie 缺 Secure：{raw}"


async def test_http_request_gets_non_secure_cookie(app, monkeypatch):
    """本地 HTTP 必须**不带** Secure。

    否则浏览器不会回传该 cookie，本地每请求都重建匿名身份——会话恢复在本地
    根本没法验证，而「本地跑不了」会反过来把这条安全要求给删掉。
    """
    _override_setting(monkeypatch, "anon_cookie_secure", None)
    raw = await _cookie_for(app, "http")
    assert "secure" not in raw.lower(), f"本地 HTTP 下 cookie 不该带 Secure：{raw}"


async def test_env_override_forces_secure_on_http(app, monkeypatch):
    """网关未传 X-Forwarded-Proto 时的兜底：ANON_COOKIE_SECURE=1 强制开启。

    生产若因网关配置拿不到 scheme，自动推导会退化成「不下发 Secure」——这是
    静默的安全降级，必须有一个显式开关兜住。
    """
    _override_setting(monkeypatch, "anon_cookie_secure", "1")
    raw = await _cookie_for(app, "http")
    assert "secure" in raw.lower(), f"ANON_COOKIE_SECURE=1 未生效：{raw}"


async def test_env_override_can_force_secure_off(app, monkeypatch):
    """反向覆盖也要可用（本地/测试环境显式关掉，避免依赖 scheme 推导）。"""
    _override_setting(monkeypatch, "anon_cookie_secure", "0")
    raw = await _cookie_for(app, "https")
    assert "secure" not in raw.lower(), f"ANON_COOKIE_SECURE=0 未生效：{raw}"


async def test_forwarded_proto_enables_secure_behind_tls_gateway(app, monkeypatch):
    """网关终止 TLS 时 ASGI 看到的是 http，真实 scheme 只在 X-Forwarded-Proto 里。"""
    _override_setting(monkeypatch, "anon_cookie_secure", None)
    raw = await _cookie_for(app, "http", headers={"X-Forwarded-Proto": "https"})
    assert "secure" in raw.lower(), f"未采信 X-Forwarded-Proto：{raw}"


async def test_direct_https_ignores_spoofed_forwarded_proto(app, monkeypatch):
    """直连 TLS 优先：伪造 X-Forwarded-Proto 不得把 HTTPS 请求降级成非 Secure。

    若实现无条件采信转发头，客户端只要发 ``X-Forwarded-Proto: http`` 就能让
    自己的 cookie 丢掉 Secure —— 安全属性不该由被保护方指定。
    """
    _override_setting(monkeypatch, "anon_cookie_secure", None)
    raw = await _cookie_for(app, "https", headers={"X-Forwarded-Proto": "http"})
    assert "secure" in raw.lower(), f"转发头把直连 HTTPS 降级了：{raw}"
