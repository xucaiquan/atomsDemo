"""项目归属身份的服务端派生（设计文档 2026-09-20 S1.1）。

不变量 1 —— 归属由服务端派生，请求体永不参与：

    登录 JWT 的 sub > 签名 cookie > X-Atoms-Anon 请求头 > 服务端新签发

客户端提供的任何字段（含历史遗留的 ``owner_key``）不得影响归属。
匿名标识为 HMAC 签名（nonce.sig，共 49 字符），客户端无法伪造有效签名；
cookie + 请求头双通道传输，消除「线上网关吃掉 Set-Cookie 就静默失效」的单点。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.auth import AccessTokenError, decode_access_token
from core.config import settings

logger = logging.getLogger(__name__)

ANON_COOKIE = "atoms_anon"
ANON_HEADER = "X-Atoms-Anon"
ANON_MAX_AGE = 180 * 24 * 3600  # 180 天

bearer_scheme = HTTPBearer(auto_error=False)

# JWT_SECRET_KEY 未配置时的兜底签名密钥：保证匿名标识在重启后仍可验签，
# 避免老访客一夜之间「项目消失」。生产部署平台始终注入 JWT_SECRET_KEY，
# 该兜底仅覆盖本地/演示环境。
_FALLBACK_SECRET = "atoms-studio-anon-fallback-key"

# 登录身份归属键里保留的 sha256 十六进制字符数："user:" + 32 = 37 ≤ 64。
_SUBJECT_CHARS = 32

# hex 字符集：用于签名/nonce 的**形状**校验（只校验长度不够，见 _verify）。
_HEX_CHARS = frozenset("0123456789abcdef")

# 「密钥缺失」只告警一次：_sign 在每条匿名路径上都会跑，每请求一条 WARNING
# 会刷屏并淹没真正重要的日志。
_warned_missing_secret = False


def _secret() -> bytes:
    """签名密钥。未配置时回落兜底密钥并**告警一次**。

    降级本身是刻意的（宁可匿名身份可伪造，也不能让所有匿名路由 500），但运维
    必须能从日志发现这件事——兜底密钥是公开仓库里的常量，任何人都能伪造他人
    匿名身份。
    """
    global _warned_missing_secret
    configured = getattr(settings, "jwt_secret_key", None)
    if configured:
        return configured.encode()
    if not _warned_missing_secret:
        _warned_missing_secret = True
        logger.warning(
            "JWT_SECRET_KEY 未配置，匿名归属键回落使用公开兜底密钥签名——"
            "任何人可伪造他人匿名身份。仅限本地/演示环境使用，生产必须注入 JWT_SECRET_KEY"
        )
    return _FALLBACK_SECRET.encode()


def _owner_key_from_subject(subject: str) -> str:
    """把任意长度的 ``sub`` 映射成定长且单射的登录归属键。

    **不能用** ``f"user:{sub}"[:64]``：``sub`` 来自 ``users.id``（String(255)），
    截断会让共享长前缀的不同用户塌缩成同一个归属键——A 登录后能看到 B 的项目。
    已本地实证：三个不同 sub（长度 80 / 60 / 60）→ 1 个 owner_key，且 60 字符
    就足够碰撞，无需极端输入。这是**数据泄露**，不是外观问题。

    sha256 的前 32 个十六进制字符 = 128 位，碰撞概率可忽略；键长恒为 37 与
    ``sub`` 长度无关。代价：由旧截断式派生出的历史 ``user:`` 项目在切换后不再
    归属到该用户（匿名 ``anon:`` 键不走这条路径，不受影响）。
    """
    digest = hashlib.sha256(subject.encode()).hexdigest()[:_SUBJECT_CHARS]
    return f"user:{digest}"


@dataclass(frozen=True)
class OwnerContext:
    """一次请求解析出的归属身份。"""

    owner_key: str  # "user:<sha256(sub)[:32]>" | "anon:<nonce>.<sig>"
    anon_key: Optional[str]  # 匿名身份时为 raw（nonce.sig）；登录身份时为 None


def _sign(nonce: str) -> str:
    return hmac.new(_secret(), nonce.encode(), hashlib.sha256).hexdigest()[:16]


def _verify(raw: Optional[str]) -> Optional[str]:
    """校验匿名标识签名；通过返回 nonce，否则 None。

    任何非法输入都必须**返回 None**，绝不抛异常：本函数在 ``get_owner`` 依赖里
    被调用，异常逃逸会变成 500（而非 401），访客用空值就能打崩任意路由。
    """
    if not raw or not isinstance(raw, str):
        return None
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    nonce, sig = parts
    # 长度校验挡住「超长 nonce + 有效签名」——验签只保证「这个 nonce 是你签过的」，
    # 不保证 nonce 有多长；不挡就会产出突破 projects.owner_key String(64) 的归属键。
    if len(nonce) != 32 or len(sig) != 16:
        return None
    # 形状校验：长度合法还不够。"z"*32 长度正好 32，_sign 也能算出签名，
    # hmac.compare_digest 于是认可它——但这偏离了 _issue() 的输出规范。
    if not _HEX_CHARS.issuperset(nonce) or not _HEX_CHARS.issuperset(sig):
        return None
    if not hmac.compare_digest(_sign(nonce), sig):
        return None
    return nonce


def _issue() -> str:
    """签发新的匿名标识：nonce(32) + '.' + sig(16) = 49 字符。"""
    nonce = secrets.token_hex(16)
    return f"{nonce}.{_sign(nonce)}"


# ------------------------------------------------------------------ cookie 传输策略
#
# Secure 属性由**当前请求的实际 scheme** 决定，而不是写死：本地开发与测试跑在
# http 上，若强制 Secure，浏览器不会回传该 cookie，匿名身份每请求都重建，
# 本地根本没法验证会话恢复。
#
# 事实来源放在这里有个现实原因：签发 cookie 的 routers/atoms.py::_json() 只拿到
# (payload, ctx)，拿不到 Request；而 get_owner 是**所有** atoms 路由共享的依赖，
# 每个请求都会经过它。由它把 scheme 记进 ContextVar，_json 直接读取即可，无需要
# 穿透二十余处调用点。ContextVar 是任务局部的，请求之间不会串味。
_request_scheme: ContextVar[str] = ContextVar("atoms_request_scheme", default="")


def _effective_scheme(request: Request) -> str:
    """本次请求对外呈现的 scheme。

    网关终止 TLS 时 ASGI 看到的 scheme 是 ``http``，真实 scheme 只在
    ``X-Forwarded-Proto`` 里。**直连 TLS 优先**：一旦 ASGI 自己就报告 https，
    就不再采信可被客户端伪造的转发头——否则一个 ``X-Forwarded-Proto: http``
    就能把 HTTPS 请求的 cookie 降级成不带 Secure。
    """
    direct = (request.url.scheme or "").lower()
    if direct == "https":
        return "https"
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return forwarded or direct


def anon_cookie_secure() -> bool:
    """匿名 cookie 是否应带 ``Secure`` 属性。

    环境变量 ``ANON_COOKIE_SECURE`` 可覆盖自动推导（网关未传
    ``X-Forwarded-Proto`` 时的兜底）：生产设 ``1`` 强制开启，本地设 ``0``
    强制关闭。未设置时按请求实际 scheme 推导。
    """
    override = (getattr(settings, "anon_cookie_secure", "") or "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    return _request_scheme.get() == "https"


def _from_client_value(raw: Optional[str]) -> Optional[OwnerContext]:
    """验签客户端携带的匿名标识（cookie 或请求头），通过则复用其身份。"""
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > 49:
        return None
    if _verify(candidate) is None:
        return None
    return OwnerContext(owner_key=f"anon:{candidate}", anon_key=candidate)


async def get_owner(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> OwnerContext:
    """解析请求的归属身份。

    签发策略（§8 偏离 1）：所有 atoms 路由共用这一个依赖；凡解析不出有效
    标识的请求，一律视为新匿名会话并签发（幂等）。不采用「只在
    list_projects/create_project 下发」的写法——generate 也可能是入口
    （旧标签页直接提交），统一后无分支、不依赖前端调用顺序。

    JWT 解析失败（AccessTokenError / 密钥未配置）**不报错**，落到匿名分支，
    保证未登录用户始终可用（FR-014）。
    """
    # 记录本次请求的 scheme，供 _json() 推导 cookie 的 Secure 属性（见 anon_cookie_secure）。
    _request_scheme.set(_effective_scheme(request))

    # ① 登录 JWT
    if credentials and credentials.credentials:
        try:
            payload = decode_access_token(credentials.credentials)
            sub = payload.get("sub")
            if sub:
                return OwnerContext(owner_key=_owner_key_from_subject(str(sub)), anon_key=None)
        except AccessTokenError:
            pass
        except Exception:  # noqa: BLE001 - 任何 token 异常都不得阻断匿名访问
            logger.debug("access token 解析异常，回落到匿名身份")

    # ② 签名 cookie
    ctx = _from_client_value(request.cookies.get(ANON_COOKIE))
    if ctx:
        return ctx

    # ③ X-Atoms-Anon 请求头（双通道）
    ctx = _from_client_value(request.headers.get(ANON_HEADER))
    if ctx:
        return ctx

    # ④ 均无效：新签发，由路由层经 Set-Cookie + 响应体回传
    raw = _issue()
    return OwnerContext(owner_key=f"anon:{raw}", anon_key=raw)
