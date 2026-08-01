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

验签怎么做：优先本地 HS256（配了 SUPABASE_JWT_SECRET 时，无网络往返）；
否则把令牌交给 Supabase 的 /auth/v1/user 去验。后者跟签名算法无关，
只需要 publishable key，理由见 _verify_via_supabase 的注释。
"""
import os
import time
from typing import Optional

import requests

import jwt
from fastapi import Depends, HTTPException, Request

# Supabase 项目设置 → API → JWT Settings → JWT Secret
SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
# Supabase 签发的令牌 aud 固定是 "authenticated"
SUPABASE_JWT_AUDIENCE = os.environ.get("SUPABASE_JWT_AUDIENCE", "authenticated")

# 项目 URL。配了它（加上 anon key）就走「交给 Supabase 验」那条路。
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
# publishable / anon key。公开值，前端包里本来就有一份。
# 后端需要它只是为了通过 Supabase 的 API 网关，不用它做任何鉴权。
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "").strip()

# 两条路任意一条配上就算启用认证
AUTH_ENABLED = bool(SUPABASE_JWT_SECRET or (SUPABASE_URL and SUPABASE_ANON_KEY))

def require_auth_configured() -> None:
    """启动时自检：真部署但没配认证 → 直接拒绝启动。

    判据是「数据库不是本地 SQLite」。本地开发用 SQLite、不需要认证；
    一旦 DATABASE_URL 指向远程 Postgres，就说明这是能被公网访问的部署，
    此时没有 JWT 密钥是严重的配置错误，不能放过去。
    """
    from .models import IS_SQLITE

    if SUPABASE_URL and not SUPABASE_ANON_KEY:
        raise RuntimeError(
            "拒绝启动：配了 SUPABASE_URL 但没有 SUPABASE_ANON_KEY。"
            "验令牌要调 Supabase 的 /auth/v1/user，那个端点在 API 网关后面，"
            "不带 apikey 头会被拒。请补上 SUPABASE_ANON_KEY"
            "（就是前端用的那个 sb_publishable_... ，公开值，不是 secret key）。"
        )
    if not IS_SQLITE and not AUTH_ENABLED:
        raise RuntimeError(
            "拒绝启动：DATABASE_URL 指向远程数据库（说明这是公网部署），"
            "但没有配置任何认证方式。这样启动的话所有接口都不需要登录，"
            "你的实盘记录和资金曲线会完全暴露。请在部署平台补上 "
            "SUPABASE_URL + SUPABASE_ANON_KEY（推荐），或 SUPABASE_JWT_SECRET。"
        )


_introspect_cache: dict = {}
_INTROSPECT_TTL = 60          # 秒。令牌本身有效期一小时，缓存一分钟足够削掉绝大部分往返


def _verify_via_supabase(token: str) -> dict:
    """把令牌交给 Supabase 自己去验：GET /auth/v1/user。

    为什么用这条路而不是本地验签：Supabase 的签名方式有两代（HS256 对称 /
    ES256 非对称），而**两代都拿不到可用的公开验签材料**——
      · 新版取公钥的端点实测返回 "Secret API key required"，
        为了拿一个公钥反而得存一个高权限的 secret key，本末倒置；
      · 旧版的 HS256 密钥在新项目里可能根本不存在。
    本地验签因此需要先搞清楚项目是哪一代、再配对应的密钥，配错的表现
    是所有请求 401 而报错看不出原因。

    交给 Supabase 验则跟签名方式完全无关：令牌有效它返回用户，无效返回 401。
    只需要 publishable key（公开值）。代价是一次网络往返，用缓存削掉——
    同一个令牌在 TTL 内只问一次。

    权衡说明：这样撤销令牌最多延迟 TTL 秒才生效。对单用户的自用系统，
    60 秒完全可以接受；本地验签其实也一样有这个问题（令牌到期前无法撤销），
    所以并没有变差。
    """
    now = time.time()
    hit = _introspect_cache.get(token)
    if hit and hit[0] > now:
        return hit[1]

    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"},
            timeout=8,
        )
    except requests.RequestException as e:
        # 网络抖动不该表现成「登录失效」把用户踢回登录页，用 503 区分开
        raise HTTPException(503, f"无法连接认证服务: {e}")

    if r.status_code == 401:
        raise HTTPException(401, "令牌无效或已过期，请重新登录")
    if r.status_code != 200:
        raise HTTPException(401, f"认证服务返回 {r.status_code}: {r.text[:120]}")

    user = r.json()
    claims = {"sub": user.get("id"), "email": user.get("email"), "raw": user}

    # 缓存别无限长大：单用户场景本来就没几个令牌，但刷新会不断产生新的
    if len(_introspect_cache) > 64:
        _introspect_cache.clear()
    _introspect_cache[token] = (now + _INTROSPECT_TTL, claims)
    return claims


def _decode(token: str) -> dict:
    """校验令牌，返回 claims。两条路，见下面的注释。"""
    # 配了 HS256 密钥就本地验签——没有网络往返，最快。
    # 没配就交给 Supabase 验（见 _verify_via_supabase 里为什么不走 JWKS）。
    if not SUPABASE_JWT_SECRET:
        return _verify_via_supabase(token)

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
