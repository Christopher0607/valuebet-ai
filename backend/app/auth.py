"""
Supabase Auth 校验。

设计原则：**本地零配置，云端强制认证，配错了要响。**

本地一键启动时没有任何环境变量，认证整个关闭——双击就能用，跟以前一样。
部署到公网时设了 SUPABASE_JWT_SECRET，所有写接口和数据接口都要求带有效令牌。

关键的一条：**云端配置缺失时必须拒绝启动，而不是静默放行。**
如果只写「有密钥就校验、没密钥就放过」，那么某次部署忘了配环境变量，
服务会照常起来、接口全部裸奔，而且没有任何迹象——你的实盘记录就挂在公网上了。
所以 require_auth_configured() 在检测到「用的是远程数据库（说明是真部署）
但没有 JWT 密钥」时直接抛异常，让部署失败，逼你去补配置。

Supabase 的令牌是 HS256 对称签名，密钥就是项目设置里的 JWT Secret，
所以这里不需要 RSA/JWKS 那一套，也就不依赖 cryptography 扩展。
"""
import os
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request

# Supabase 项目设置 → API → JWT Settings → JWT Secret
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
# Supabase 签发的令牌 aud 固定是 "authenticated"
SUPABASE_JWT_AUDIENCE = os.environ.get("SUPABASE_JWT_AUDIENCE", "authenticated")

# 新版 Supabase 项目用非对称签名，公钥走 JWKS。填了项目 URL 就用这条路。
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
# publishable / anon key。公开值，前端包里本来就有一份。
# 后端需要它**只是为了通过 Supabase API 网关去取公钥**，不用它做任何鉴权。
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()
JWKS_URL = f"{SUPABASE_URL}/auth/v1/.well-known/jwks.json" if SUPABASE_URL else ""

# 两条路任意一条配上就算启用认证
AUTH_ENABLED = bool(SUPABASE_JWT_SECRET or JWKS_URL)

_jwk_cache = None


def _jwk_client():
    """PyJWKClient 自带公钥缓存，所以只建一次——每次请求都新建的话，
    每个 API 调用都要多一次到 Supabase 的网络往返。

    **必须带 apikey 头。** Supabase 的整个 /auth/v1 都在它的 API 网关后面，
    连公开的 JWKS 端点也不例外，不带这个头会返回：
        {"message":"No API key found in request"}
    而 PyJWKClient 默认不发任何自定义头。少了它的后果是取公钥永远失败，
    表现为所有请求 401、报错写「取签名公钥失败」——看起来像密钥配错了，
    实际是缺一个请求头。

    这里用的是 publishable（anon）key，它本来就是公开的、会打进前端包，
    放在后端环境变量里没有任何额外风险。
    """
    global _jwk_cache
    if _jwk_cache is None:
        headers = {"apikey": SUPABASE_ANON_KEY} if SUPABASE_ANON_KEY else None
        _jwk_cache = jwt.PyJWKClient(JWKS_URL, cache_keys=True, headers=headers)
    return _jwk_cache


def require_auth_configured() -> None:
    """启动时自检：真部署但没配认证 → 直接拒绝启动。

    判据是「数据库不是本地 SQLite」。本地开发用 SQLite、不需要认证；
    一旦 DATABASE_URL 指向远程 Postgres，就说明这是能被公网访问的部署，
    此时没有 JWT 密钥是严重的配置错误，不能放过去。
    """
    from .models import IS_SQLITE

    if JWKS_URL and not SUPABASE_ANON_KEY:
        raise RuntimeError(
            "拒绝启动：配了 SUPABASE_URL（走 JWKS 验签）但没有 SUPABASE_ANON_KEY。"
            "Supabase 的 JWKS 端点在 API 网关后面，不带 apikey 头会返回 "
            "'No API key found in request'，导致取公钥永远失败、所有请求 401。"
            "请补上 SUPABASE_ANON_KEY（就是前端用的那个 sb_publishable_... ，公开值）。"
        )
    if not IS_SQLITE and not AUTH_ENABLED:
        raise RuntimeError(
            "拒绝启动：DATABASE_URL 指向远程数据库（说明这是公网部署），"
            "但没有设置 SUPABASE_JWT_SECRET。这样启动的话所有接口都不需要登录，"
            "你的实盘记录和资金曲线会完全暴露。请在部署平台的环境变量里补上"
            "SUPABASE_JWT_SECRET（Supabase 控制台 → Project Settings → API → JWT Secret）。"
        )


def _decode(token: str) -> dict:
    """验签。同时支持 Supabase 的两代签名方式。

    旧项目：HS256 对称签名，密钥是设置页的 JWT Secret。
    新项目：Supabase 2025 年起改用非对称签名密钥（ES256/RS256），
            公钥挂在 <project>/auth/v1/.well-known/jwks.json，
            设置页可能根本没有 "JWT Secret" 那一项。

    只支持 HS256 的话，新建的 Supabase 项目部署完会所有请求 401，
    而报错信息是「令牌无效」，看不出真正原因是算法不匹配。
    所以两条路都留：配了 SUPABASE_JWT_SECRET 走对称，
    配了 SUPABASE_URL 走 JWKS。
    """
    if JWKS_URL:
        try:
            signing_key = _jwk_client().get_signing_key_from_jwt(token)
        except Exception as e:
            raise HTTPException(401, f"取签名公钥失败: {e}")
        key, algs = signing_key.key, ["ES256", "RS256"]
    else:
        key, algs = SUPABASE_JWT_SECRET, ["HS256"]

    try:
        return jwt.decode(token, key, algorithms=algs, audience=SUPABASE_JWT_AUDIENCE)
    except jwt.ExpiredSignatureError:
        # 前端拿到 401 会自动去 Supabase 刷新令牌再重试，所以这里要跟
        # 「令牌无效」区分开——无效是要重新登录的，过期只需要刷新。
        raise HTTPException(401, "令牌已过期，请重新登录")
    except jwt.InvalidTokenError as e:
        raise HTTPException(401, f"令牌无效: {e}")


async def current_user(request: Request) -> Optional[dict]:
    """FastAPI 依赖：解析并校验 Authorization 头里的 Supabase 令牌。

    认证未启用（本地）时直接返回 None，不做任何检查。
    """
    if not AUTH_ENABLED:
        return None

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "缺少登录令牌")

    claims = _decode(auth[7:])
    return {"id": claims.get("sub"), "email": claims.get("email"), "claims": claims}


# 挂在需要保护的路由上。本地它是空操作，云端它拦住所有未登录请求。
AuthDep = Depends(current_user)
