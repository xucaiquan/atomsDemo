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
    """截断式实现会让共享长前缀的两个 sub 塌缩成同一身份（数据泄露，而非外观问题）。"""
    prefix = "p" * 300
    keys = {
        await _owner_key_for_subject(prefix + "a"),
        await _owner_key_for_subject(prefix + "b"),
    }
    assert len(keys) == 2


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
