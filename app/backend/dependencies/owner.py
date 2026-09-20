"""归属键（owner_key）的服务端派生。

对应 spec §2 不变量 1：**归属由服务端派生，请求体永不参与**。

两级身份：
- 登录用户 → ``user:{sub}``，来自平台 JWT 的 sub，客户端无法伪造。
- 匿名访客 → ``anon:{nonce}.{sig}``，nonce 由服务端随机生成，sig 是
  HMAC-SHA256 签名。客户端即使拿到 raw 也无法为别的 nonce 造出有效签名，
  因此「换个 owner_key 就能看别人的项目」这条路被堵死。

为什么匿名标识必须服务端签名：旧实现 ``lib/constants.ts::getOwnerKey()`` 用
localStorage 里的 ``anon-xxxx`` 当归属键——那是客户端可任意伪造的字符串，
且它全仓无任何调用点，等于没有隔离。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass
from typing import Optional

from core.auth import AccessTokenError, decode_access_token
from core.config import settings
from dependencies.auth import bearer_scheme
from fastapi import Depends, Request

logger = logging.getLogger(__name__)

ANON_COOKIE = "atoms_anon"
ANON_HEADER = "X-Atoms-Anon"
# 180 天：匿名标识丢失意味着用户丢掉自己的项目列表，给足有效期。
ANON_MAX_AGE = 180 * 24 * 3600

_SIG_CHARS = 16
_NONCE_BYTES = 16
# user: 键取 sha256 前 32 个十六进制字符（128 bit）——足够避免碰撞，且总长固定 37。
_SUBJECT_CHARS = 32

# 空密钥降级只告警一次：每请求告警会把日志刷爆。
_warned_missing_secret = False


def _secret() -> bytes:
    """签名密钥复用平台 JWT 密钥；未配置时返回空串（签名仍确定，但不安全）。

    必须用 ``getattr`` 兜底：``settings.__getattr__`` 在环境变量缺失时**抛
    AttributeError** 而不是返回空串，直接取属性会让 ``JWT_SECRET_KEY`` 未配置的
    部署在每个路由上 500，与 get_owner 「密钥未配置仍按匿名身份工作」的约定冲突。

    空密钥意味着匿名归属键任何人都能算出来，因此首次降级时告警一次，
    让运维能发现，而不是无声无息地退化。
    """
    global _warned_missing_secret
    secret = getattr(settings, "jwt_secret_key", "") or ""
    if not secret and not _warned_missing_secret:
        _warned_missing_secret = True
        logger.warning("JWT 密钥未配置：匿名归属键的签名已降级为可预测值，本部署下匿名身份可被伪造，请配置 JWT_SECRET_KEY")
    return secret.encode("utf-8")


def _user_key(subject: str) -> str:
    """由 sub 派生定长归属键。

    必须**始终**哈希，不能只在 sub 过长时哈希：``users.id`` 是 String(255)，
    sub 可远超 ``projects.owner_key`` 的 64 字符上限，直接拼接会让写入失败。
    也不能截断 sub：共享长前缀的两个 sub 会塌缩成同一身份（数据泄露）。
    哈希同时保证长度恒为 37 且两个不同 sub 得到两个不同键。
    """
    return f"user:{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:_SUBJECT_CHARS]}"


def _sign(nonce: str) -> str:
    return hmac.new(_secret(), nonce.encode("utf-8"), hashlib.sha256).hexdigest()[:_SIG_CHARS]


def _verify(raw: Optional[str]) -> Optional[str]:
    """校验签名，通过则返回 nonce，否则返回 None。"""
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    nonce, sig = parts
    if not nonce or not sig:
        return None
    expected = _sign(nonce)
    if not hmac.compare_digest(sig, expected):
        return None
    return nonce


def _issue() -> str:
    nonce = secrets.token_hex(_NONCE_BYTES)
    return f"{nonce}.{_sign(nonce)}"


@dataclass(frozen=True)
class OwnerContext:
    """本次请求的归属身份。

    ``anon_key`` 非空表示这是匿名身份，调用方应把它回传客户端
    （Set-Cookie 与 GET /projects 的响应体），让它下次带回来。
    """

    owner_key: str
    anon_key: Optional[str] = None


async def get_owner(
    request: Request,
    credentials=Depends(bearer_scheme),
) -> OwnerContext:
    """按优先级派生归属键：登录 sub > cookie > 请求头 > 新签发。

    fail-closed：查不到归属时不是拒绝，而是签发一个新匿名身份——新身份查不到
    任何既有项目，效果等价于拒绝，但不会把「第一次访问」误伤。
    """
    # ① 登录用户优先。token 无效时**不报错**，落到匿名分支——
    #    密钥未配置或 token 过期的部署仍能按匿名身份工作。
    if credentials:
        try:
            payload = decode_access_token(credentials.credentials)
            subject = payload.get("sub")
            if subject:
                return OwnerContext(owner_key=_user_key(subject), anon_key=None)
        except AccessTokenError:
            logger.debug("JWT 校验未通过，回退到匿名身份")
        except Exception as exc:  # noqa: BLE001 - 校验异常不得中断请求
            logger.warning("解析凭据时出现意外异常: %s", type(exc).__name__)

    # ② cookie（HttpOnly，防 XSS 窃取）
    # ③ 请求头（cookie 被网关吃掉时的退路）
    for candidate in (
        request.cookies.get(ANON_COOKIE),
        request.headers.get(ANON_HEADER),
    ):
        nonce = _verify(candidate)
        if nonce:
            return OwnerContext(owner_key=f"anon:{candidate}", anon_key=candidate)

    # ④ 新签发
    raw = _issue()
    return OwnerContext(owner_key=f"anon:{raw}", anon_key=raw)
