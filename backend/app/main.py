"""
FastAPI application. Run with:  uvicorn app.main:app --reload --port 8000
See README.md in the project root for full setup instructions.
"""
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import desc, insert
from datetime import datetime
from pydantic import BaseModel
from typing import Optional, List
import logging
import os
import hmac
import threading

from . import updater
from .models import (
    init_db, get_db, Match, Prediction, Odds, Bet, RealBet, UserSettings,
    Competition, UpdateLog, BayesianTeamStateRow, ParlayBet, ParlayLeg, PriceLog,
    Withdrawal, MarketOdds,
    SessionLocal,
)
from .updater import run_full_update
from .scheduler import start_scheduler, next_run_info, update_is_due
from .auth import AUTH_ENABLED, current_user, require_auth_configured, AuthDep
from .model import expected_value, kelly_pct, BayesianTeamState, parlay_ev_and_risk, suggest_parlays
from .ev_evidence import (
    bet_advisory, parlay_advisory, price_capture, reality_check,
    advisory_from_three_odds, vig_from_odds, BREAKEVEN_VIG,
    _roi_at_capture, CAPTURE_BY_LEGS, TYPICAL_PARLAY_MARGIN_PER_LEG,
)

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="ValueBet Local API")

# /api/matches 一次返回全部比赛，实测 0.88 MB。本地无所谓，手机流量下
# 明显能感觉到。JSON 压缩率很高（这份压完约十分之一），一行中间件的事。
# 500 字节以下不压——小响应压完反而更大，还白搭 CPU。
app.add_middleware(GZipMiddleware, minimum_size=500)

# 打包后的前端由本进程同源托管，手机访问走的是同源请求，不需要 CORS。
# 这里只为「开发时用 vite 热更新」放行：本机，外加局域网私有网段的 5173，
# 方便在手机上调试开发版。刻意不写 allow_origins=["*"]——服务现在绑在
# 0.0.0.0，同网段的任何设备都能访问，没必要再把跨域也全开。
app.add_middleware(
    CORSMiddleware,
    # 云端前端域名通过 FRONTEND_ORIGINS 环境变量传进来，逗号分隔，
    # 可以填多个——比如同时挂在 Vercel 和 Netlify 上（后者是为了某些
    # 网络环境下 Vercel 连不上时留一条备用链接），两个域名都能正常用。
    # 不写死单个域名是因为 Vercel 每个预览部署都有独立域名。
    allow_origins=[o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]
                  + ["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_origin_regex=r"http://(192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}):5173",
    allow_methods=["*"],
    allow_headers=["*"],
    # ETag 必须显式暴露，否则跨域时前端**读不到**这个响应头。
    # CORS 默认只让 JS 看到六个「简单」响应头，ETag 不在其中——
    # allow_headers=["*"] 管的是请求头，跟这个是两回事。
    # 这正是那种"本地怎么测都对、一部署就失效"的坑：本地前端是同源
    # （后端自己托管 dist），没有 CORS，res.headers.get('etag') 一直有值；
    # 云端前端在 Vercel、后端在 Render，跨域，同一行代码拿到的是 null，
    # 于是每次都当成"没有 ETag"重新下载 4.8 MB，优化完全失效且不报错。
    expose_headers=["ETag"],
)


# 公开路径：不需要登录也能访问。刻意只放这三类——
#   /api/health  探活，部署平台要用
#   /docs /openapi.json  接口文档，不含任何用户数据
#   非 /api 开头的一切（前端静态文件、登录页本身）
#
# /api/cron/update 也在这里，但它**不是无保护的**：外部定时任务拿不到
# Supabase 用户令牌，所以它绕过 JWT 中间件，改用 CRON_SECRET 自己校验
# （见 cron_update）。放进这个名单只是让请求走到那个函数里去，
# 真正的门在函数内部——没配 CRON_SECRET 时它拒绝所有调用。
_PUBLIC_PREFIXES = ("/api/health", "/api/cron/")


@app.middleware("http")
async def _enforce_auth(request: Request, call_next):
    """统一拦截，而不是在 30 个路由上逐个挂依赖。

    逐个挂的问题不是麻烦，是**漏挂不会报错**——以后新增一个接口忘了加，
    它就默默地不需要登录，而你不会发现。中间件是默认拒绝、显式放行，
    新接口自动受保护。
    """
    if not AUTH_ENABLED or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if request.url.path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)
    if request.method == "OPTIONS":            # CORS 预检不带令牌，必须放行
        return await call_next(request)
    try:
        await current_user(request)
    except HTTPException as e:
        return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


@app.get("/api/health")
def health():
    """探活。不碰数据库，部署平台用它判断实例是否存活。"""
    return {"ok": True, "auth_enabled": AUTH_ENABLED}


@app.on_event("startup")
def on_startup():
    # 真部署但没配认证 → 直接拒绝启动，不能让接口裸奔
    require_auth_configured()
    init_db()
    _seed_default_competition()
    _seed_default_settings()
    start_scheduler(interval_hours=12)


def _seed_default_competition():
    """
    登记所有赛事。data_source 里的 {season} 占位符由 _resolve_data_source()
    在抓取时替换——它会先探测当前赛季（7月之后猜下一个新赛季），文件不存在
    就自动回退到上一个完整赛季，所以新赛季一发布就会自动接上，不用改代码。

    赛事 code 必须跟 model.py 里 COMPETITION_SCOPE / COMPETITION_NEUTRAL 的
    键对应，否则会走兜底参数，产出所有球队都一样的空洞预测（不报错，要留意）。
    """
    from .models import SessionLocal
    db = SessionLocal()
    BASE = "https://raw.githubusercontent.com/openfootball/football.json/master/{season}"
    try:
        specs = [
            ("wc2026", "2026 FIFA World Cup", "2026世界杯",
             "https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json", True),
            ("epl", "English Premier League", "英超", f"{BASE}/en.1.json", True),
            ("laliga", "La Liga", "西甲", f"{BASE}/es.1.json", True),
            ("seriea", "Serie A", "意甲", f"{BASE}/it.1.json", True),
            ("bundesliga", "Bundesliga", "德甲", f"{BASE}/de.1.json", True),
            # 法甲/英冠跟上面四个走同一套 openfootball 文件命名与抓取路径。
            # 注意两者的「未来赛程」可用性不同，实测（2026-08）：
            #   英冠 2026-27 的 .txt 赛程已发布 → 有未来比赛可预测/下注
            #   法甲 2026-27 三个源全部 404（.json 镜像和 .txt 都没有）
            #     → 目前只有历史数据，等 openfootball 发布后会自动接上，
            #       在那之前「预测」页看不到法甲未来比赛是正确行为
            ("ligue1", "Ligue 1", "法甲", f"{BASE}/fr.1.json", True),
            ("championship", "EFL Championship", "英冠", f"{BASE}/en.2.json", True),
            # 英甲/英乙/西乙。.json 镜像的赛季有真实断档（en.3/en.4 只有 6 季、
            # es.2 有 8 季），抓取时缺的赛季静默跳过，不影响。
            ("leagueone", "EFL League One", "英甲", f"{BASE}/en.3.json", True),
            ("leaguetwo", "EFL League Two", "英乙", f"{BASE}/en.4.json", True),
            ("segunda", "LaLiga 2", "西乙", f"{BASE}/es.2.json", True),
            # 全国联赛（英格兰第五级）比较特殊：football.json 镜像**没有**
            # en.5（逐季验过全是 404），所以 data_source 直接写 .txt 模板。
            # _resolve_data_source 对每个赛季是「先试 .json 再试 .txt」，
            # 这里 .json 那一路必然 404，会自动落到下面 _TXT_SOURCES 那条。
            # 英格兰全国联赛停用（2026-08，用户要求下架）。停的是**界面和抓取**，
            # 不是数据：
            #   · 库里 296 场已赛记录原样保留，把下面这个 True 改回去就全回来
            #   · 训练完全不受影响——train_mle_club.py 读的是
            #     historical_results_club.csv（en.5 有 3,371 场），不是数据库。
            #     英乙↔全国联赛那批共享球队的桥照样在，英超到第五级的阶梯不断
            # 停它的实际理由：第五级半职业联赛，博彩公司不开盘，The Odds API
            # 的 45 个 soccer key 里没有它，openfootball 也还没发 2026-27，
            # 所以它既没有未来赛程也没有赔率，摆在界面上只是占位置。
            ("nationalleague", "National League", "英格兰全国联赛",
             f"{BASE}/en.5.json", False),
            # 意乙。跟英甲/英乙/西乙同一类：历史走 openfootball，2026-27 的赛程
            # 上游还没发，先由 The Odds API 顶上（见 _ODDS_FIXTURE_COMPETITIONS）。
            # data_source 用 .json 模板只是给 _resolve_data_source 认赛季用，
            # 真正取数会自动选中 _TXT_SOURCES 里那条 .txt——it.2 的 .json 镜像
            # 2021-24 三季是 404，只有 .txt 是全的。
            ("serieb", "Serie B", "意乙", f"{BASE}/it.2.json", True),
            # 德乙、法乙。跟意乙同一批接入，训练数据早就在 club 参数表里了
            # （德乙 4,077 场、法乙 4,122 场），这次是把它们注册成完整赛事。
            # 德乙的 .json 镜像是全的；法乙跟意乙一样 2021-24 三季 404，
            # 所以 _TXT_SOURCES 里给它挂了 .txt，探测阶梯会自动选中。
            ("bundesliga2", "2. Bundesliga", "德乙", f"{BASE}/de.2.json", True),
            ("ligue2", "Ligue 2", "法乙", f"{BASE}/fr.2.json", True),
            ("ucl", "UEFA Champions League", "欧冠", f"{BASE}/uefa.cl.json", True),
            # 这两个数据源不是 openfootball，走完全不同的抓取路径（见
            # updater.py 的 run_full_update 里对 code 的分支判断），data_source
            # 这里只是个非空占位（get_active_competitions 靠它是否为 None
            # 过滤），不会被当成 URL 模板去替换 {season} 或请求。
            ("mls", "Major League Soccer", "美职联", "api-football:mls", True),
            ("efl_cup", "English League Cup", "英格兰联赛杯", "api-football:efl_cup", True),
        ]
        for code, name, name_zh, src, active in specs:
            existing = db.query(Competition).filter_by(code=code).first()
            if existing:
                # 数据源可能会更新（比如换了路径），已存在的记录也同步一下
                existing.data_source = src
                existing.is_active = active
            else:
                db.add(Competition(code=code, name=name, name_zh=name_zh,
                                   data_source=src, is_active=active))

        # 清掉早期那个没有数据源的欧冠占位记录，避免和上面真正的 'ucl' 重复
        stale = db.query(Competition).filter_by(code="ucl2627").first()
        if stale:
            db.delete(stale)

        db.commit()
    finally:
        db.close()


def _seed_default_settings():
    """本地模式（没有登录）预先建好那一份设置，跟以前的行为一样——
    双击就能用，不需要等第一次接口调用才懒创建。云端各账号的设置行
    由 _get_or_create_settings() 在第一次用到时按需建，没法在启动时
    枚举有哪些账号。"""
    from .models import SessionLocal
    db = SessionLocal()
    try:
        if not db.query(UserSettings).filter_by(setting_key=_LOCAL_OWNER).first():
            db.add(UserSettings(setting_key=_LOCAL_OWNER))
            db.commit()
    finally:
        db.close()


# ══════════════════════════════════════════════════════════
# 账号数据隔离
# ══════════════════════════════════════════════════════════
#
# 起因：Bet / RealBet / ParlayBet / UserSettings / Withdrawal 这几张表
# 一开始完全没有"属于谁"的概念——所有登录账号查的是同一份注单、同一条
# 资金曲线、同一份凯利设置。本地单人用没问题，一旦云端开了多账号登录，
# 表现就是账号 A 能看到、甚至能取消账号 B 的下注，"实盘"页显示的是
# 所有人的钱混在一起。这一段就是补上这个隔离。

_LOCAL_OWNER = "local"          # 本地未登录模式的固定归属键，行为跟以前完全一样


def _owner_key(user: Optional[dict]) -> str:
    """账号数据隔离键。云端用 Supabase 的 user id；本地没有登录概念，
    固定用 "local"。"""
    return user["id"] if user else _LOCAL_OWNER


def _owned(query, model, owner: str):
    """给查询加上"这行属于当前账号"的过滤条件。

    本地模式（AUTH_ENABLED 为假）额外放行 owner_id 为空的历史行——
    这套隔离是这次才加上的，之前所有本地数据都没有 owner_id，不放行的话
    单人本地用户会觉得自己的历史记录一夜之间全部消失了。

    云端模式没有这个例外：owner_id 为空的行只可能是账号隔离上线前、
    测试阶段留下的数据，归属不明，直接当不可见处理——这比瞎猜它属于
    哪个账号、或者让所有账号都看得到要安全。
    """
    if not AUTH_ENABLED:
        return query.filter((model.owner_id == owner) | (model.owner_id.is_(None)))
    return query.filter(model.owner_id == owner)


def _is_owned(row, owner: str) -> bool:
    """单行版本的 _owned()，用在取消/删除接口的权限检查上。"""
    if row.owner_id == owner:
        return True
    return not AUTH_ENABLED and row.owner_id is None


# ══════════════════════════════════════════════════════════
# 条件请求（ETag / 304）
# ══════════════════════════════════════════════════════════
#
# 起因：前端每次打开页面都会先用 localStorage 的缓存把界面画出来，然后
# 后台再把十个接口重拉一遍（横幅上那句"显示的是上次的数据，正在后台更新"
# 就是这个阶段）。实测 9,500 场的库里，这十个接口一共 4,885 KB，其中
# /api/matches 一个人占 4,880 KB、耗时占 87%——而里面 8,550 场是**已经
# 踢完、永远不会再变**的历史比赛。等于每次进网站都把同一份历史重新下载
# 一遍。免费档 Render + 远端 Postgres 上这就是那段肉眼可见的等待。
#
# 做法是标准的 HTTP 条件请求：响应带 ETag，客户端下次带 If-None-Match，
# 没变就回 304（空体）。省掉的不只是流量，还有服务端序列化 9,500 个对象
# 的 CPU——算指纹只要几条聚合查询。
#
# 指纹怎么算才**不会漏**（这是唯一有风险的地方，漏了就是用户看到旧数据）：
#   · 比赛和预测只在 run_full_update 里被改动，而它每跑一轮必写一行
#     UpdateLog。所以 UpdateLog 的最大 id + ran_at 就覆盖了这两张表的
#     任何变化，包括"比分填进去了""rps 回填了"这种 count 不变的原地修改。
#   · 再叠上 matches 的行数 / 最大 id / 最大 updated_at 作为第二道保险，
#     万一将来有人绕开 run_full_update 直接写库也能察觉。
#   · latest_odds 是按账号隔离的，用户自己 POST 赔率不经过 UpdateLog，
#     所以单独把该账号 Odds 的行数和最大 id 也算进去。
#   · 停用赛事会改变返回的比赛集合，把 is_active 的状态也算进去。
# 宁可多变（多下载一次，只是慢一点）也不能少变（看到旧数据）。
_ETAG_VERSION = "v1"


def _data_version(db: Session, owner: str) -> str:
    from sqlalchemy import func

    log_id, log_at = db.query(func.max(UpdateLog.id), func.max(UpdateLog.ran_at)).one()
    n_match, max_match, match_at = db.query(
        func.count(Match.id), func.max(Match.id), func.max(Match.updated_at)).one()
    # 预测必须连 updated_at 一起看：比赛踢完后回填 rps / is_correct、重训后
    # 重算概率，全都是原地修改，count 和 max(id) 一动不动。
    # 另外单独数一次「已经有 rps 的行数」——历史行的 updated_at 是这次加列
    # 之后才回填的，在被改到之前是 NULL，这个计数能兜住那段过渡期。
    n_pred, max_pred, pred_at = db.query(
        func.count(Prediction.id), func.max(Prediction.id),
        func.max(Prediction.updated_at)).one()
    n_scored = db.query(func.count(Prediction.id)).filter(
        Prediction.rps.isnot(None)).scalar()
    n_mkt, max_mkt = db.query(func.count(MarketOdds.id), func.max(MarketOdds.id)).one()
    n_odds, max_odds = _owned(
        db.query(func.count(Odds.id), func.max(Odds.id)), Odds, owner).one()
    # 停用的赛事 id 拼进去——停用/启用会改变 /api/matches 返回的比赛集合，
    # 而它不走 run_full_update（是启动时 seeding 改的），UpdateLog 察觉不到。
    inactive = ",".join(str(c.id) for c in db.query(Competition.id).filter(
        Competition.is_active == False).order_by(Competition.id).all())   # noqa: E712
    return "|".join(str(x) for x in (
        _ETAG_VERSION, log_id, log_at, n_match, max_match, match_at,
        n_pred, max_pred, pred_at, n_scored, n_mkt, max_mkt,
        n_odds, max_odds, inactive))


def _etag(*parts) -> str:
    """指纹本身可能带空格/冒号（datetime 的字符串形式），不能直接当 ETag——
    HTTP 要求它是加了引号的 token。哈希一遍既规范又短。"""
    import hashlib
    return '"' + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:24] + '"'


def _not_modified(request: Request, tag: str) -> bool:
    """客户端手上那份是不是就是这一份。

    If-None-Match 允许带多个值、也允许 W/ 弱校验前缀，所以要拆开逐个比，
    不能整串相等——只按整串比的话，浏览器自己加了 W/ 前缀就永远不命中，
    ETag 等于白做。
    """
    header = request.headers.get("if-none-match")
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == tag or candidate == "*":
            return True
    return False


def _conditional(request: Request, tag: str) -> Optional[Response]:
    """命中就返回 304 响应，没命中返回 None（调用方照常算数据）。

    用 Response 不用 JSONResponse：304 按 RFC 9110 不能带响应体，而
    JSONResponse(content=None) 会实实在在写出四个字节的 "null"。
    多数客户端会忽略它，但那是"碰巧没事"，不是对的。
    """
    if _not_modified(request, tag):
        return Response(status_code=304, headers={
            "ETag": tag,
            # no-cache 不是"不要缓存"，是"可以存，但每次用之前必须回来问一次"。
            # 正是想要的行为：浏览器留着上次的响应体，每次只发一个几百字节的
            # 条件请求。写 no-store 才是真的禁用缓存。
            "Cache-Control": "no-cache",
        })
    return None


def _get_or_create_settings(db: Session, owner: str) -> UserSettings:
    """按账号取设置（资金总额、凯利比例……），没有就建一份默认的。

    云端账号是运行时才登录进来的，没法像本地那样在启动时预先建好，
    只能在第一次用到的时候懒创建。"""
    s = db.query(UserSettings).filter_by(setting_key=owner).first()
    if not s:
        s = UserSettings(setting_key=owner)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


# ══════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════

# updater.run_full_update() 实际会产出四种状态，但 models.py 里 UpdateLog.status
# 那一行的注释只写了 'ok' | 'error'，于是 /api/status 把四种状态原样丢给前端、
# 前端也就只会分「有没有报错」两种。后果是**部分失败长得跟全部成功一模一样**：
# 某个赛事的数据源 404（休赛期新赛季文件还没发布是常态），updater 已经老老实实
# 把 status 记成 'partial'、把失败的赛事列进 detail 了，界面上却照样是绿的，
# 用户看到的是「更新成功」+ 少了一整个联赛的赛程，还以为是那个联赛没有比赛。
#
# 这张表是状态语义的唯一权威处：前端只读 severity/label，不要自己去 if 状态字符串；
# updater 以后新增状态值也只改这一处。
#
# 'skipped' 的特殊情况（这是 updater.py 现存的一个局限，不是这里的疏漏）：
# run_full_update() 抢不到并发锁时**直接 return，压根不写 UpdateLog 行**，
# 所以 /api/status 永远读不到 'skipped'——它只会出现在 POST /api/update-now
# 的即时返回值里。这里仍然保留这一项，是为了让前端能用同一张表去解释
# update-now 的返回值。要让 /api/status 也能看到 skipped，得改 updater.py
# 让它落一行日志，那个文件不在本次改动范围内，先把限制写清楚。
_UPDATE_STATUS_MEANINGS = {
    "ok":      {"severity": "ok",      "label": "全部赛事更新成功"},
    "partial": {"severity": "warning", "label": "部分赛事抓取失败，其余赛事已更新"},
    "error":   {"severity": "error",   "label": "更新失败，数据可能是旧的"},
    "skipped": {"severity": "warning", "label": "上一次更新还在跑，本次被跳过（数据没变）"},
}


@app.get("/api/status")
def status(db: Session = Depends(get_db)):
    last_run = db.query(UpdateLog).order_by(desc(UpdateLog.ran_at)).first()
    raw_status = last_run.status if last_run else None

    if last_run is None:
        meaning = {"severity": "unknown", "label": "后端还没有跑过更新"}
    else:
        meaning = _UPDATE_STATUS_MEANINGS.get(
            raw_status,
            # 认不出来的状态不要静悄悄当成正常，否则以后 updater 加了新状态，
            # 这里又会退回到「部分失败看起来像成功」的老毛病
            {"severity": "unknown", "label": f"未知状态 '{raw_status}'"},
        )

    return {
        "last_update": last_run.ran_at.isoformat() if last_run else None,
        "last_status": raw_status,               # 原字符串，老前端还在读，只增不改
        "last_detail": last_run.detail if last_run else None,
        # 下面几个是新增的：把「这次更新到底算成功还是算警告」讲明白
        "last_severity": meaning["severity"],    # 'ok' | 'warning' | 'error' | 'unknown'
        "last_status_label": meaning["label"],
        "last_ok": raw_status == "ok",           # 只有全绿才 True——partial 不是成功
        "last_counts": {
            "matches_updated": last_run.matches_updated,
            "predictions_updated": last_run.predictions_updated,
            "bets_resolved": last_run.bets_resolved,
        } if last_run else None,
        "status_meanings": _UPDATE_STATUS_MEANINGS,
        "next_scheduled_update": next_run_info(),
        # 现在有一轮更新正在跑吗。前端点了「立即更新」之后靠它判断什么时候
        # 收工——接口是立刻返回的（202），真正的活在后台线程里。
        #
        # 直接读 updater 那个模块级锁，不新增状态：多存一份「正在跑」的标志
        # 就多一个会跟真实情况不同步的地方（线程崩了没人去清标志，界面就
        # 永远显示"更新中"）。锁是那件事本身，不是它的影子。
        "update_running": updater._update_lock.locked(),
        "note": "next_scheduled_update is null if this backend process was just restarted — "
                "the schedule only exists while this process is running.",
        "status_note": "last_status 可能是 ok / partial / error；partial 表示有赛事抓取失败，"
                       "失败清单在 last_detail 里。skipped 只会出现在 POST /api/update-now 的"
                       "返回值中（并发跳过时 updater 不写日志行），这里读不到。",
    }


# ══════════════════════════════════════════════════════════
# 更新的三个触发口
# ══════════════════════════════════════════════════════════
#
# 背景：进程内那个「每 12 小时」的定时任务在 Render 免费档上等于不存在
# （闲置 15 分钟休眠，进程活不满 12 小时，见 scheduler.py 顶部）。
# 实测后果是界面上「上次更新」停在四天前。所以云端真正的定时更新走
# 外部触发：GitHub Actions 定时打 POST /api/cron/update。

CRON_SECRET = os.environ.get("CRON_SECRET", "").strip()

# 外部定时触发的最小间隔。跟 scheduler 的 12 小时一致，也就是 DEPLOY.md
# 估算 The Odds API 免费额度（500 次/月）时用的「每天 2 次」。
_CRON_MIN_HOURS = 12


def _run_update_in_background() -> bool:
    """把一轮完整更新丢到后台线程。已经有一轮在跑就不重复起。

    为什么不能同步跑：一轮更新要抓十几个赛事再写库，冷启动还要先花约
    50 秒把休眠的实例叫醒。同步跑的话这个请求会挂两分钟以上，GitHub
    Actions 的 curl、浏览器的 fetch、以及中间任何一层代理都可能先超时——
    连接断了，服务端却还在跑，调用方拿不到任何结果，看起来就是「点了没反应」。
    前端那个 120 秒超时就是这么被撞上的。

    返回 False 表示「已经有一轮在跑」，调用方据此回 409 而不是假装启动了。
    这个判断是**尽力而为**的：locked() 到线程真正 acquire 之间有窗口，两个
    同时到达的请求可能都看到"没锁"。真正的互斥在 run_full_update 里面——
    后到的那个拿不到锁，直接返回 skipped，不会有两轮同时写库。这里的 409
    只是为了在常见情况下给出更准确的回应，不承担正确性。

    daemon=True：进程退出时不等这个线程。Render 免费档本来就是说停就停，
    假装能优雅收尾没有意义；跑到一半没写成 UpdateLog 的那一轮，下次触发时
    闸门会判定"还该跑"，自然会补上。
    """
    if updater._update_lock.locked():
        return False

    def _job():
        db = SessionLocal()
        try:
            result = run_full_update(db)
            logging.getLogger("valuebet.updater").info("[trigger] 更新结束: %s", result)
        except Exception as e:
            # run_full_update 内部已经把绝大多数异常吞掉并写进 UpdateLog 了。
            # 能漏到这里的基本只有「连 UpdateLog 都写不进去」——数据库拒写
            # （比如 Supabase 配额受限）就是这种。这时库里不会留下任何痕迹，
            # 所以这条日志是唯一的线索，不能不记。
            logging.getLogger("valuebet.updater").error(
                "[trigger] 更新失败且没能写进 update_log: %s", str(e)[:300])
        finally:
            db.close()

    threading.Thread(target=_job, name="valuebet-update", daemon=True).start()
    return True


@app.post("/api/update-now")
def update_now(force: bool = True):
    """手动触发（界面上那个「立即更新」按钮）。

    立刻返回，不等更新跑完——前端改成轮询 /api/status 看进度。
    默认 force=True：人主动点的，就是想现在更新，不该被 12 小时闸门挡住。
    """
    if not force and not update_is_due(_CRON_MIN_HOURS):
        return JSONResponse(
            {"status": "skipped", "detail": f"距上次更新不足 {_CRON_MIN_HOURS} 小时"},
            status_code=200)
    if not _run_update_in_background():
        return JSONResponse(
            {"status": "already_running", "detail": "已经有一轮更新在跑，这次不重复启动"},
            status_code=409)
    return JSONResponse({"status": "started"}, status_code=202)


@app.post("/api/cron/update")
def cron_update(request: Request, force: bool = False):
    """给外部定时任务用的触发口（GitHub Actions 每 12 小时打一次）。

    认证走 CRON_SECRET，不走 Supabase JWT——GitHub Actions 拿不到用户令牌。
    这个路径在 _PUBLIC_PREFIXES 里，绕过了 JWT 中间件，所以**这里的校验是
    唯一的一道门**，写松了就是把「让后端跑一轮全量更新」开放给全网。

    没配 CRON_SECRET 时拒绝所有调用，不是放行。跟 auth.py 里那条既定原则
    一致：配置缺失要响，不能静默放行——否则某次部署忘了配环境变量，
    接口就默默裸奔了，而且没有任何迹象。
    """
    if not CRON_SECRET:
        return JSONResponse(
            {"detail": "服务端没有配置 CRON_SECRET，这个接口被禁用。"
                       "要用的话在部署平台的环境变量里配一个随机长字符串，"
                       "并在调用方用同一个值。"},
            status_code=503)

    supplied = request.headers.get("X-Cron-Key", "")
    # compare_digest 而不是 ==：字符串比较会在第一个不同的字符处提前返回，
    # 逐字节的耗时差可以被用来一位一位地把密钥试出来。
    if not hmac.compare_digest(supplied, CRON_SECRET):
        return JSONResponse({"detail": "X-Cron-Key 不正确"}, status_code=403)

    # 闸门：定时任务可能因为重试、手动触发、跟进程内那个 IntervalTrigger
    # 撞上而多跑几次。不足 12 小时就当空操作，省 Odds API 配额和 Supabase
    # egress。真要强跑传 ?force=true。
    if not force and not update_is_due(_CRON_MIN_HOURS):
        return JSONResponse(
            {"status": "skipped",
             "detail": f"距上次更新不足 {_CRON_MIN_HOURS} 小时，这次跳过"},
            status_code=200)

    if not _run_update_in_background():
        return JSONResponse(
            {"status": "already_running", "detail": "已经有一轮更新在跑"},
            status_code=409)
    return JSONResponse({"status": "started"}, status_code=202)


@app.get("/api/update-log")
def update_log(db: Session = Depends(get_db)):
    logs = db.query(UpdateLog).order_by(desc(UpdateLog.ran_at)).limit(20).all()
    return [{
        "ran_at": l.ran_at.isoformat(), "status": l.status,
        "matches_updated": l.matches_updated, "predictions_updated": l.predictions_updated,
        "bets_resolved": l.bets_resolved, "detail": l.detail,
    } for l in logs]


# ══════════════════════════════════════════════════════════
# COMPETITIONS
# ══════════════════════════════════════════════════════════

@app.get("/api/competitions")
def list_competitions(include_inactive: bool = False, db: Session = Depends(get_db)):
    """默认只回启用中的赛事。

    停用的赛事（is_active=False）数据全都还在库里，只是不该再出现在界面上——
    否则赛事筛选条上会挂着一个点进去什么都没有的按钮。想看全部（比如确认
    停用的那个还在不在）传 include_inactive=true。
    """
    q = db.query(Competition)
    if not include_inactive:
        q = q.filter(Competition.is_active == True)     # noqa: E712  SQLAlchemy 需要 == True
    return [{
        "id": c.id, "code": c.code, "name": c.name, "name_zh": c.name_zh, "is_active": c.is_active,
    } for c in q.all()]


# ══════════════════════════════════════════════════════════
# MATCHES + PREDICTIONS
# ══════════════════════════════════════════════════════════

@app.get("/api/matches")
def list_matches(request: Request, status_filter: Optional[str] = None,
                 competition_id: Optional[int] = None,
                 db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    # 数据没变就回 304，省掉 4.8 MB 的重复下载（见上面 _data_version 那段）。
    # 查询参数必须算进指纹：?status_filter=upcoming 只回未来赛程，跟不带参数
    # 的全量是两份不同的东西，共用一个 ETag 会让客户端拿错。
    tag = _etag(_data_version(db, _owner_key(user)), "matches", status_filter, competition_id)
    hit = _conditional(request, tag)
    if hit:
        return hit

    # 停用赛事的比赛不回给前端。不这么做的话，赛事筛选条上虽然没有它的按钮，
    # 「全部」那个计数里却还包含它的几百场，回测页也照样列出来——等于只藏了
    # 一半，看起来像 bug。数据在库里一条没删，改回 is_active=True 就全回来。
    inactive_ids = [c.id for c in db.query(Competition.id).filter(
        Competition.is_active == False).all()]      # noqa: E712
    q = db.query(Match)
    if inactive_ids:
        q = q.filter(~Match.competition_id.in_(inactive_ids))
    if status_filter:
        q = q.filter(Match.status == status_filter)
    if competition_id:
        q = q.filter(Match.competition_id == competition_id)
    matches = q.order_by(Match.date).all()

    # 赛事名一次性查出来做成字典，不要在循环里 m.competition.name 那样取——
    # 那是 lazy load，1739 场比赛就是 1739 次额外查询（N+1）。
    # 显示名的取法跟 /api/backtest-summary 保持一致：优先中文名，没有才退回英文，
    # 两处不一致的话前端按赛事分组时会出现「英超」和「English Premier League」
    # 两个看起来不同、其实是同一个赛事的分组。
    comps_all = db.query(Competition).all()
    comp_names = {c.id: (c.name_zh or c.name) for c in comps_all}

    # ── 这条预测背后到底有多少真实数据 ──────────────────────────
    #
    # 起因：联赛杯这类淘汰赛，大量球队一季只踢 1-2 场，样本量够不上单独
    # 拟合 MLE 参数的门槛（≥6场），dixon_coles 查不到就静默退回
    # FALLBACK_ATTACK/DEFENSE = (0,0)「联赛平均水平」。问题不在于退回本身，
    # 而在于**界面上完全看不出区别**——一场两队都没有任何数据的比赛，
    # 照样显示「53.8% / 20.5% / 25.8%」这种看起来跟真实预测一模一样的数字。
    #
    # 拿真实数据量过一遍联赛杯首轮 35 场（2026-08 实测）：
    #   双方都有 MLE 拟合参数        7 场
    #   至少一方只有贝叶斯后验       26 场（6-10 场观测，偏薄但有真实依据）
    #   至少一方零信息(flat prior)   2 场（Barnet / Oldham Athletic，
    #                                 2022-2024 压根没参加过这项赛事）
    #
    # 所以分三档暴露给前端，让"这个数字有多可信"变成可见的：
    #   full —— 双方都有 MLE 拟合参数
    #   thin —— 至少一方只有贝叶斯后验（先验是联赛平均，靠少量真实比赛修正过）
    #   none —— 至少一方两者都没有，纯 flat prior，这个数字没有意义
    #
    # 刻意不在这里隐藏比赛：赛程本身是真的，用户可能就是想看这场几点踢、
    # 自己填赔率。藏掉反而会让人以为系统漏抓了。标注出来、把判断交回给用户，
    # 跟项目「精算式的诚实」的基调一致。
    from .model import load_params, scope_for_competition
    comp_scope = {c.id: scope_for_competition(c.code) for c in comps_all}
    _mle_cache = {}

    def _mle_known(scope: str):
        if scope not in _mle_cache:
            try:
                _mle_cache[scope] = set(load_params(scope)["_index"].keys())
            except Exception:
                _mle_cache[scope] = set()
        return _mle_cache[scope]

    bayes_known = {
        (r.team_name, r.competition_id)
        for r in db.query(BayesianTeamStateRow.team_name,
                          BayesianTeamStateRow.competition_id).all()
    }

    def _backing(m) -> str:
        known = _mle_known(comp_scope.get(m.competition_id, "international"))
        tiers = []
        for t in (m.team1, m.team2):
            if (t or "").strip().lower() in known:
                tiers.append("mle")
            elif (t, m.competition_id) in bayes_known:
                tiers.append("bayes")
            else:
                tiers.append("none")
        if "none" in tiers:
            return "none"
        return "full" if all(x == "mle" for x in tiers) else "thin"

    # 预测和赔率也必须批量取。上面那段注释为赛事名避开了 N+1，紧接着的循环
    # 却又犯了两次同样的错：每场比赛查一次 Prediction、再查一次 Odds，
    # 1739 场就是 3478 次往返。
    #
    # 本地 SQLite 完全看不出来——它是进程内函数调用，几千次也就一秒。
    # 换成云端的远端 Postgres，每次往返十几到几十毫秒，同一个接口要跑
    # 几十秒甚至超时，页面就一直卡在「加载中」。这个 bug 只在部署后出现。
    match_ids = [m.id for m in matches]

    preds = {}
    latest_odds_by_match = {}
    market_by_match = {}
    # 分批：SQLite 的 IN 参数个数有上限（老版本 999），Postgres 宽松得多，
    # 取小的那个才两边都安全
    for i in range(0, len(match_ids), 900):
        chunk = match_ids[i:i + 900]
        for p in db.query(Prediction).filter(Prediction.match_id.in_(chunk)).all():
            preds[p.match_id] = p
        # 按 (match_id, recorded_at 倒序) 排好，每场第一条就是最新的那条赔率，
        # setdefault 只保留第一条。按账号过滤——这是"你自己填过的赔率"，
        # 不应该预填出别的账号看到的报价。
        odds_q = _owned(db.query(Odds), Odds, _owner_key(user)).filter(Odds.match_id.in_(chunk))
        for o in odds_q.order_by(Odds.match_id, desc(Odds.recorded_at)).all():
            latest_odds_by_match.setdefault(o.match_id, o)
        # 市场报价**不**过 _owned：它是公开数据，没有归属。走 _owned 的话
        # 云端模式会因为 owner_id 为空把它全判成不可见，自动抓回来的赔率
        # 反而一条都显示不出来。
        for mo in db.query(MarketOdds).filter(MarketOdds.match_id.in_(chunk)).all():
            market_by_match[mo.match_id] = mo

    out = []
    for m in matches:
        pred = preds.get(m.id)
        latest_odds = latest_odds_by_match.get(m.id)
        mo = market_by_match.get(m.id)
        out.append({
            "id": m.id, "competition_id": m.competition_id,
            "competition_name": comp_names.get(m.competition_id),
            "date": m.date.isoformat(), "team1": m.team1, "team2": m.team2,
            "score1": m.score1, "score2": m.score2,
            "time_utc": m.time_utc,
            "round": m.round, "grp": m.grp, "ground": m.ground, "status": m.status,
            "prediction": (dict(_pred_dict(pred), data_backing=_backing(m))
                           if pred else None),
            "latest_odds": {
                "odds_home": latest_odds.odds_home, "odds_draw": latest_odds.odds_draw, "odds_away": latest_odds.odds_away,
            } if latest_odds else None,
            # 市场公开报价，跟上面「你自己填的」分开。前端拿它预填输入框
            # 和比价，下注真正用的还是用户确认后的那个价。
            "market_odds": {
                "n_books": mo.n_books,
                "best_home": mo.best_home, "best_draw": mo.best_draw, "best_away": mo.best_away,
                "avg_home": mo.avg_home, "avg_draw": mo.avg_draw, "avg_away": mo.avg_away,
                "fetched_at": mo.fetched_at.isoformat() if mo.fetched_at else None,
            } if mo else None,
        })
    # 带上 ETag，下次客户端就能拿它来问"变了没有"。
    return JSONResponse(out, headers={"ETag": tag, "Cache-Control": "no-cache"})


def _pred_dict(p: Prediction):
    """只回前端真正会读的字段。

    原来还回 attack_home / defense_home / attack_away / defense_away /
    predicted 五个字段——前端**一处都没用**（全仓库 grep 过：
    prob_* 8 次、is_correct 3 次、rps 2 次、data_backing 2 次、xg_* 各 1 次，
    这五个 0 次）。它们照样跟着每一场比赛发一遍。

    以前无所谓，现在有所谓了：接入 6 个联赛之后 /api/matches 从 1,700 场
    涨到 5,900 场，这五个字段等于凭空多发近三万个数。手机上流量和 JSON
    解析都是实打实的成本。

    数据库里照旧存着这五个字段（Prediction 表没动），要按球队查 attack/
    defense 有 /api/bayesian-states，要看单场的完整拆解有
    /api/score-distribution/{match_id}——都在，只是不该让每一场比赛都
    顺带发一份没人读的副本。
    """
    return {
        "prob_home": p.prob_home, "prob_draw": p.prob_draw, "prob_away": p.prob_away,
        "xg_home": p.xg_home, "xg_away": p.xg_away,
        "is_correct": p.is_correct, "rps": p.rps,
    }


# ══════════════════════════════════════════════════════════
# ODDS + EV CALCULATION
# ══════════════════════════════════════════════════════════

class OddsInput(BaseModel):
    match_id: int
    odds_home: float
    odds_draw: Optional[float] = None
    odds_away: float
    # 只算 EV、不落库。默认 True 保持原行为不变。
    #
    # 需要它是因为市场赔率现在会自动预填进输入框：卡片一展开，前端就会拿
    # 预填值请求一次 EV，如果照存，用户根本没碰过的市场价就变成了"他自己
    # 填的价"。后果不只是名义上的——latest_odds 会永久盖过 market_odds，
    # 那场比赛显示的价从此冻在展开的那一刻，再也不跟着市场更新。
    # 所以：值还等于市场预填时传 save=false，用户改过了才落库。
    save: bool = True


class OddsBulkInput(BaseModel):
    items: List[OddsInput]


@app.post("/api/odds/bulk")
def submit_odds_bulk(payload: OddsBulkInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """一次存多场的赔率，只写一个事务。

    为什么需要它：串关页点「生成推荐组合」时会把选中比赛的赔率存回后端，
    原来是**每场发一个 POST /api/odds**。以前串关候选池里只有用户手填过
    赔率的那几场，撑死十几个请求，没人觉得有问题；接进市场赔率自动导入
    之后，候选池一下变成上百场，用户真的选了 115 场——于是变成 115 个
    并发请求、460 次 SQL、115 次写事务。

    浏览器对同一域名并发上限约 6 条，云端连接池只有 5+5 条，115 个请求
    先在浏览器排 19 批、再在服务端抢连接，Render 免费档的请求超时一到就
    切断连接，前端看到的就是 "Failed to fetch"。这是实测复现出来的，
    不是推测（见 validation/23_bulk_odds_regression.py）。

    这里刻意**不返回 EV**：调用方（串关页）本来就把返回值丢掉了
    （原代码 .catch(() => null)），单场页要算 EV 继续走 POST /api/odds。
    少算一遍 EV 也省掉每场两次查询。
    """
    if not payload.items:
        return {"saved": 0, "skipped": 0}
    owner = _owner_key(user)
    ids = [it.match_id for it in payload.items]
    known = {row.id for row in db.query(Match.id).filter(Match.id.in_(ids)).all()}

    rows = [{
        "match_id": it.match_id, "source": "manual", "owner_id": owner,
        "odds_home": it.odds_home, "odds_draw": it.odds_draw, "odds_away": it.odds_away,
        "recorded_at": datetime.utcnow(),
    } for it in payload.items if it.match_id in known]
    skipped = len(payload.items) - len(rows)

    if rows:
        # 走 Core 的 executemany，不用 ORM 的 db.add()。差别是实测出来的：
        # 115 行用 db.add() 是 231 条 SQL（ORM 要逐行取回自增主键），
        # 这里是 2 条。本地 SQLite 差 165ms 看不太出来，云端远端 Postgres
        # 上每条 SQL 都是一次网络往返，229 次和 2 次是完全不同的量级。
        db.execute(insert(Odds), rows)
    db.commit()
    return {"saved": len(rows), "skipped": skipped}


@app.post("/api/odds")
def submit_odds(payload: OddsInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    match = db.query(Match).filter_by(id=payload.match_id).first()
    if not match:
        raise HTTPException(404, "Match not found")

    if payload.save:
        db.add(Odds(
            match_id=payload.match_id, source="manual", owner_id=_owner_key(user),
            odds_home=payload.odds_home, odds_draw=payload.odds_draw, odds_away=payload.odds_away,
        ))
        db.commit()

    pred = db.query(Prediction).filter_by(match_id=payload.match_id).first()
    if not pred:
        raise HTTPException(400, "No prediction available for this match yet")

    settings = _get_or_create_settings(db, _owner_key(user))
    frac, cap = settings.kelly_fraction, settings.max_bet_pct

    ev_home = expected_value(pred.prob_home, payload.odds_home)
    ev_away = expected_value(pred.prob_away, payload.odds_away)
    ev_draw = expected_value(pred.prob_draw, payload.odds_draw) if payload.odds_draw else None

    k_home = kelly_pct(pred.prob_home, payload.odds_home, frac, cap)
    k_away = kelly_pct(pred.prob_away, payload.odds_away, frac, cap)
    k_draw = kelly_pct(pred.prob_draw, payload.odds_draw, frac, cap) if payload.odds_draw else None

    return {
        "ev_home": round(ev_home, 4), "ev_draw": round(ev_draw, 4) if ev_draw is not None else None, "ev_away": round(ev_away, 4),
        "kelly_home": round(k_home, 4), "kelly_draw": round(k_draw, 4) if k_draw is not None else None, "kelly_away": round(k_away, 4),
        "kelly_home_amount": round(k_home * settings.bankroll_total, 2),
        "kelly_draw_amount": round(k_draw * settings.bankroll_total, 2) if k_draw is not None else None,
        "kelly_away_amount": round(k_away * settings.bankroll_total, 2),
    }


# ══════════════════════════════════════════════════════════
# VIRTUAL BETS
# ══════════════════════════════════════════════════════════

class BetInput(BaseModel):
    model_config = {"protected_namespaces": ()}

    match_id: int
    outcome: str          # 'home' | 'draw' | 'away'
    stake: float
    odds_used: float
    ev_at_bet: Optional[float] = None
    kelly_pct: Optional[float] = None
    model_prob: Optional[float] = None


@app.get("/api/bets")
def list_bets(db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    bets = _owned(db.query(Bet), Bet, _owner_key(user)).order_by(desc(Bet.created_at)).all()
    return [_bet_dict(b) for b in bets]


def _bet_dict(b: Bet):
    m = b.match
    return {
        "id": b.id, "match_id": b.match_id,
        # bets 表本身没有 competition_id 这一列（跟 real_bets 不同），赛事只能顺着
        # match 取。少了这个字段，虚拟盘那一页就没有任何办法按赛事分组或筛选——
        # 现在库里 6 个赛事 1739 场比赛混在一张表里，不给赛事就只是一堆队名。
        "competition_id": m.competition_id if m else None,
        "team1": m.team1 if m else None, "team2": m.team2 if m else None, "date": m.date.isoformat() if m else None,
        "outcome": b.outcome, "stake": b.stake, "odds_used": b.odds_used,
        "ev_at_bet": b.ev_at_bet, "kelly_pct": b.kelly_pct, "result": b.result, "pnl": b.pnl,
        "created_at": b.created_at.isoformat(),
    }


@app.post("/api/bets")
def create_bet(payload: BetInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    bet = Bet(**payload.dict(), result="pending", owner_id=_owner_key(user))
    db.add(bet)
    db.commit()
    db.refresh(bet)
    return _bet_dict(bet)


@app.delete("/api/bets/{bet_id}")
def cancel_bet(bet_id: int, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """取消一笔还没结算的虚拟下注——手滑点错、或者想换个方向重下。

    只允许取消 pending 的。已经结算过的不能删：那条记录已经算进了
    /api/bankroll-summary 的资金曲线和胜率统计，删掉等于悄悄改写历史战绩，
    跟"取消一笔还没发生的事"完全是两回事。
    """
    bet = db.query(Bet).filter_by(id=bet_id).first()
    if not bet or not _is_owned(bet, _owner_key(user)):
        # 不属于自己的注单一律当"不存在"，不要用 403——403 等于告诉对方
        # "这个 id 是存在的，只是不是你的"，一样会泄露别人的下注量。
        raise HTTPException(404, "下注记录不存在")
    if bet.result != "pending":
        raise HTTPException(400, "已结算的下注不能取消，那会悄悄改掉历史战绩和资金曲线")
    db.delete(bet)
    db.commit()
    return {"status": "cancelled", "id": bet_id}


# ══════════════════════════════════════════════════════════
# REAL (LIVE-MONEY) BETS — manually entered after you place them
# ══════════════════════════════════════════════════════════

class RealBetInput(BaseModel):
    model_config = {"protected_namespaces": ()}

    match_id: int
    competition_id: Optional[int] = None
    platform: str = "bk8"
    outcome: str
    stake_real: float
    currency: str = "HKD"
    odds_used: float
    model_prob_at_bet: Optional[float] = None
    ev_at_bet: Optional[float] = None
    kelly_suggested_pct: Optional[float] = None
    kelly_suggested_amount: Optional[float] = None


@app.get("/api/real-bets")
def list_real_bets(db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    bets = _owned(db.query(RealBet), RealBet, _owner_key(user)).order_by(desc(RealBet.placed_at)).all()
    return [_real_bet_dict(b) for b in bets]


def _real_bet_dict(b: RealBet):
    m = b.match
    return {
        "id": b.id, "match_id": b.match_id,
        # real_bets 表有自己的 competition_id 列，但它是 nullable 的，而
        # RealBetInput 里这个字段是 Optional——前端不传就是 NULL。所以读的时候
        # 要往 match 上兜一层，否则历史注单（以及任何没传该字段的客户端写进来的
        # 注单）在实盘页会全部落进「未知赛事」，按赛事分组直接失效。
        # 用注单自己存的值优先：万一将来出现「注单归属赛事 ≠ 比赛所属赛事」的
        # 情况（比如同一场比赛在两个赛事里都登记过），显式写进来的那个才是准的。
        "competition_id": b.competition_id if b.competition_id is not None else (m.competition_id if m else None),
        "team1": m.team1 if m else None, "team2": m.team2 if m else None, "date": m.date.isoformat() if m else None,
        "platform": b.platform, "outcome": b.outcome, "stake_real": b.stake_real, "currency": b.currency,
        "odds_used": b.odds_used, "ev_at_bet": b.ev_at_bet,
        "kelly_suggested_amount": b.kelly_suggested_amount,
        "result": b.result, "pnl_real": b.pnl_real, "payout_real": b.payout_real,
        "placed_at": b.placed_at.isoformat(),
        "settled_at": b.settled_at.isoformat() if b.settled_at else None,
    }


@app.post("/api/real-bets")
def create_real_bet(payload: RealBetInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    data = payload.dict()

    # 前端没传赛事就从比赛回填，把值真的写进库里，而不是只在读的时候兜底。
    # 差别在于：只兜底的话 real_bets.competition_id 永远是 NULL，以后想直接用
    # SQL 按赛事筛实盘注单（比如「只看欧冠的实盘 ROI」）会一条都查不到。
    if data.get("competition_id") is None:
        m = db.query(Match).filter_by(id=data["match_id"]).first()
        if m:
            data["competition_id"] = m.competition_id

    kelly_amt = data.get("kelly_suggested_amount")
    followed = None
    if kelly_amt:
        followed = abs(data["stake_real"] - kelly_amt) < kelly_amt * 0.15
    bet = RealBet(**data, actually_followed_kelly=followed, result="pending", owner_id=_owner_key(user))
    db.add(bet)
    db.commit()
    db.refresh(bet)
    return _real_bet_dict(bet)


@app.delete("/api/real-bets/{bet_id}")
def cancel_real_bet(bet_id: int, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """取消一笔还没结算的实盘登记。

    只是撤销这里的记录，不碰你在 BK8 等平台上的真实注单——如果那边的注
    还在，去那边自己也要取消/让它照常结算，这里只是同步你自己的记账。
    只允许取消 pending 的，理由跟 cancel_bet 一样：已结算的删了会悄悄
    改写历史战绩和资金曲线。
    """
    bet = db.query(RealBet).filter_by(id=bet_id).first()
    if not bet or not _is_owned(bet, _owner_key(user)):
        raise HTTPException(404, "实盘记录不存在")
    if bet.result != "pending":
        raise HTTPException(400, "已结算的下注不能取消，那会悄悄改掉历史战绩和资金曲线")
    db.delete(bet)
    db.commit()
    return {"status": "cancelled", "id": bet_id}


# ══════════════════════════════════════════════════════════
# SETTINGS (custom bankroll / Kelly fraction / caps)
# ══════════════════════════════════════════════════════════

class SettingsInput(BaseModel):
    bankroll_total: float
    kelly_fraction: float
    max_bet_pct: float
    min_ev_threshold: float


@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    s = _get_or_create_settings(db, _owner_key(user))
    return {
        "bankroll_total": s.bankroll_total, "kelly_fraction": s.kelly_fraction,
        "max_bet_pct": s.max_bet_pct, "min_ev_threshold": s.min_ev_threshold,
    }


@app.put("/api/settings")
def update_settings(payload: SettingsInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    s = _get_or_create_settings(db, _owner_key(user))
    for k, v in payload.dict().items():
        setattr(s, k, v)
    db.commit()
    return {"status": "saved"}


# ══════════════════════════════════════════════════════════
# BANKROLL SUMMARY (for the chart)
# ══════════════════════════════════════════════════════════

@app.get("/api/bankroll-summary")
def bankroll_summary(db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """
    资金走势 + 汇总。资金池是全局共用的（所有赛事共享一个 bankroll_total），
    盈亏统计按虚拟盘/实盘分开，但不按赛事分开——赛事维度的统计在
    /api/backtest-summary，那个是模型预测准确率，跟资金流向是两回事。

    串关盈亏计入对应的虚拟/实盘曲线（一注串关算一个资金事件）。

    提款只影响资金曲线，不影响「盈亏」「ROI」「胜率」这些统计——提款是
    把已经赢到的钱转出去，不是一笔新的输赢，混进盈亏统计里会把 ROI
    算得莫名其妙地低。只作用于实盘，虚拟盘没有提款这回事。
    """
    owner = _owner_key(user)
    settings = _get_or_create_settings(db, owner)
    base = settings.bankroll_total

    v_bets = _owned(db.query(Bet), Bet, owner).filter(Bet.result != "pending").all()
    r_bets = _owned(db.query(RealBet), RealBet, owner).filter(RealBet.result != "pending").all()
    parlays = _owned(db.query(ParlayBet), ParlayBet, owner).filter(ParlayBet.result != "pending").all()
    withdrawals = _owned(db.query(Withdrawal), Withdrawal, owner).all()

    # 把所有已结算的资金事件收敛成 (日期, 盈亏, 虚拟还是实盘) 三元组。
    # 提款用同样的形状塞进去（金额取负、kind 固定 "real"）——merge 循环
    # 不需要为它单独分支，跟处理一笔亏损的注单没有任何区别。
    events = []
    for b in v_bets:
        events.append((b.created_at.date(), b.pnl or 0, "virtual"))
    for b in r_bets:
        events.append((b.placed_at.date(), b.pnl_real or 0, "real"))
    for p in parlays:
        events.append((p.created_at.date(), p.pnl or 0, "virtual" if p.kind == "virtual" else "real"))
    for w in withdrawals:
        events.append((w.withdrawn_at.date(), -w.amount, "real"))

    events.sort(key=lambda e: e[0])

    # 合并成单一数据集，每个点同时带 virtual 和 real 两个值。
    # 之前是两个独立数组分别喂给两条 Line，配 category 类型的 X 轴时
    # recharts 会因为两边日期集合不同而画歪——这是曲线看起来有问题的原因之一。
    # 另一个原因是起点被写死成「今天」，后面的点却是比赛的历史日期，
    # 导致线从今天往回画。现在起点取最早一笔注单的日期。
    start_date = events[0][0] if events else datetime.utcnow().date()
    series = [{"date": start_date.isoformat(), "virtual": base, "real": base}]

    v_running = base
    r_running = base
    for ev_date, pnl, kind in events:
        if kind == "virtual":
            v_running += pnl
        else:
            r_running += pnl
        series.append({
            "date": ev_date.isoformat(),
            "virtual": round(v_running, 2),
            "real": round(r_running, 2),
        })

    v_pnl = sum(b.pnl or 0 for b in v_bets) + sum(p.pnl or 0 for p in parlays if p.kind == "virtual")
    r_pnl = sum(b.pnl_real or 0 for b in r_bets) + sum(p.pnl or 0 for p in parlays if p.kind == "real")
    v_staked = sum(b.stake for b in v_bets) + sum(p.stake for p in parlays if p.kind == "virtual")
    r_staked = sum(b.stake_real for b in r_bets) + sum(p.stake for p in parlays if p.kind == "real")

    v_wins = sum(1 for b in v_bets if b.result == "win") + sum(1 for p in parlays if p.kind == "virtual" and p.result == "win")
    r_wins = sum(1 for b in r_bets if b.result == "win") + sum(1 for p in parlays if p.kind == "real" and p.result == "win")
    total_withdrawn = sum(w.amount for w in withdrawals)

    return {
        "bankroll_base": base,
        "series": series,
        "virtual": {
            "total_pnl": round(v_pnl, 2),
            "roi_pct": round(v_pnl / v_staked * 100, 2) if v_staked else 0,
            "total_bets": len(v_bets) + sum(1 for p in parlays if p.kind == "virtual"),
            "wins": v_wins,
        },
        "real": {
            "total_pnl": round(r_pnl, 2),
            "roi_pct": round(r_pnl / r_staked * 100, 2) if r_staked else 0,
            "total_bets": len(r_bets) + sum(1 for p in parlays if p.kind == "real"),
            "wins": r_wins,
            "total_withdrawn": round(total_withdrawn, 2),
            # 当前可提取余额：起始资金 + 已结算实盘盈亏 - 已提款。
            # 不扣待结算注单的本金——这跟资金曲线其余部分的口径一致，
            # 待结算的钱在这个系统里从来就不算「已经花出去」。
            "current_balance": round(base + r_pnl - total_withdrawn, 2),
        },
    }


# ══════════════════════════════════════════════════════════
# WITHDRAWALS — 实盘资金提出的记账
# ══════════════════════════════════════════════════════════
#
# 跟这个系统的其他"实盘"功能一样：这里只记账，不碰真钱。用户自己在
# BK8 之类的平台把钱转出去之后，回来这里登记一笔，好让追踪的资金曲线
# 跟真实情况对得上。见 硬性约束 "不做自动下注" ——这条同样适用：
# 这里不会、也不能替你去平台上发起真实的提现操作。

class WithdrawalInput(BaseModel):
    amount: float
    currency: str = "HKD"
    note: Optional[str] = None


def _withdrawal_dict(w: Withdrawal):
    return {
        "id": w.id, "amount": w.amount, "currency": w.currency, "note": w.note,
        "withdrawn_at": w.withdrawn_at.isoformat(),
    }


@app.get("/api/withdrawals")
def list_withdrawals(db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    rows = _owned(db.query(Withdrawal), Withdrawal, _owner_key(user)).order_by(desc(Withdrawal.withdrawn_at)).all()
    return [_withdrawal_dict(w) for w in rows]


@app.post("/api/withdrawals")
def create_withdrawal(payload: WithdrawalInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    if payload.amount <= 0:
        raise HTTPException(400, "提款金额必须大于 0")
    w = Withdrawal(amount=payload.amount, currency=payload.currency, note=payload.note, owner_id=_owner_key(user))
    db.add(w)
    db.commit()
    db.refresh(w)
    return _withdrawal_dict(w)


@app.delete("/api/withdrawals/{withdrawal_id}")
def delete_withdrawal(withdrawal_id: int, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """撤销一笔提款记录——纯粹是记错了、手滑多按一次这类账目层面的更正。

    没有 pending 概念（提款不像下注那样会"结算"），所以不需要跟
    cancel_bet 一样的状态检查，只要这行还在就能删。
    """
    w = db.query(Withdrawal).filter_by(id=withdrawal_id).first()
    if not w or not _is_owned(w, _owner_key(user)):
        raise HTTPException(404, "提款记录不存在")
    db.delete(w)
    db.commit()
    return {"status": "deleted", "id": withdrawal_id}


# ══════════════════════════════════════════════════════════
# BACKTEST SUMMARY
# ══════════════════════════════════════════════════════════

@app.get("/api/backtest-summary")
def backtest_summary(request: Request, competition_id: Optional[int] = None,
                     db: Session = Depends(get_db)):
    """
    传 competition_id → 返回该赛事的扁平统计（向后兼容旧格式）
    不传 → 返回按赛事拆分的数组，**不做任何跨赛事聚合**

    为什么不能混着算：不同赛事是不同的球队池、不同的参数表、大概率不同的
    准确率。把世界杯的 67% 和联赛的准确率平均成一个数字，那个数字哪个都
    不代表。加第二个赛事之前先修掉，而不是等数字已经明显错了才发现。
    """
    # 体积很小（2 KB）但**耗时不小**：每个赛事各做一次 Prediction join Match
    # 把全部已赛预测捞进内存再求和，实测 9,500 场的库上 211 ms，是十个接口里
    # 排第二的。它只依赖比赛和预测，跟账号无关，所以指纹用 owner 无关的那份。
    tag = _etag(_data_version(db, _LOCAL_OWNER), "backtest", competition_id)
    hit = _conditional(request, tag)
    if hit:
        return hit

    def _stats(preds):
        total = len(preds)
        correct = sum(1 for p in preds if p.is_correct)
        avg_rps = sum(p.rps or 0 for p in preds) / total if total else 0
        return total, correct, avg_rps

    if competition_id is not None:
        preds = db.query(Prediction).join(Match).filter(
            Match.status == "played", Match.competition_id == competition_id
        ).all()
        total, correct, avg_rps = _stats(preds)
        return JSONResponse({
            "competition_id": competition_id,
            "total": total, "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0,
            "avg_rps": round(avg_rps, 4),
            "random_baseline_rps": 0.245,
        }, headers={"ETag": tag, "Cache-Control": "no-cache"})

    by_competition = []
    for comp in db.query(Competition).order_by(Competition.id).all():
        preds = db.query(Prediction).join(Match).filter(
            Match.status == "played", Match.competition_id == comp.id
        ).all()
        total, correct, avg_rps = _stats(preds)
        if total == 0:
            continue          # 还没有已完赛比赛的赛事不列出来
        by_competition.append({
            "competition_id": comp.id,
            "competition_code": comp.code,
            "competition_name": comp.name_zh or comp.name,
            "total": total, "correct": correct,
            "accuracy": round(correct / total, 4),
            "avg_rps": round(avg_rps, 4),
        })

    return JSONResponse({"by_competition": by_competition, "random_baseline_rps": 0.245},
                        headers={"ETag": tag, "Cache-Control": "no-cache"})


# ══════════════════════════════════════════════════════════
# BAYESIAN TEAM STATES — surfaces the "real-time updating" behavior
# ══════════════════════════════════════════════════════════

@app.get("/api/bayesian-states")
def list_bayesian_states(competition_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    Lists every team whose Bayesian posterior has been updated at least
    once, showing both the current mean estimate and its uncertainty
    (std dev) -- the whole point of doing this Bayesian rather than
    sticking with the static MLE point estimate is that a team like
    Spain, after a run of 5-0 wins, should visibly show attack trending
    up here between matches, without needing a full MLE retrain.
    """
    q = db.query(BayesianTeamStateRow)
    if competition_id:
        q = q.filter(BayesianTeamStateRow.competition_id == competition_id)
    rows = q.order_by(BayesianTeamStateRow.updated_at.desc()).all()

    out = []
    for r in rows:
        state = BayesianTeamState.from_dict({
            "team_name": r.team_name,
            "attack_shape": r.attack_shape, "attack_rate": r.attack_rate,
            "defense_theta_shape": r.defense_theta_shape, "defense_theta_rate": r.defense_theta_rate,
            "decay": r.decay, "n_updates": r.n_updates,
        })
        out.append({
            "team_name": r.team_name, "competition_id": r.competition_id,
            "current_attack": round(state.current_attack(), 4),
            "current_defense": round(state.current_defense(), 4),
            "attack_uncertainty": state.current_attack_std(),
            "defense_uncertainty": state.current_defense_std(),
            "n_updates": r.n_updates,
            "updated_at": r.updated_at.isoformat(),
        })
    return out


# ══════════════════════════════════════════════════════════
# PARLAY (independent matches only — see model.py for why same-match
# combinations like "Spain win" + "over 2.5 goals" are NOT supported here)
# ══════════════════════════════════════════════════════════

class ParlayLegInput(BaseModel):
    match_id: int
    outcome: str  # 'home' | 'draw' | 'away'
    odds: float
    label: str


class ParlayInput(BaseModel):
    legs: list[ParlayLegInput]
    parlay_odds: float


@app.post("/api/parlay")
def calculate_parlay(payload: ParlayInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """
    Computes joint probability, EV, and Kelly stake for a parlay across
    matches the caller asserts are independent (e.g. Spain vs Italy, and
    France vs Germany — two different matches, nothing linking them).
    This endpoint does not verify independence; it's the caller's
    responsibility to only combine legs from genuinely separate matches,
    same as agreed when this feature was scoped.
    """
    if len(payload.legs) < 2:
        raise HTTPException(400, "A parlay needs at least 2 legs")

    match_ids = [leg.match_id for leg in payload.legs]
    if len(set(match_ids)) != len(match_ids):
        raise HTTPException(
            400,
            "Two legs reference the same match_id — this endpoint only supports "
            "independent legs from different matches. Same-match combinations "
            "(e.g. 'Spain win' + 'over 2.5 goals' in one match) are not "
            "independent events and require the /api/score-distribution "
            "endpoint's joint distribution instead."
        )

    settings = _get_or_create_settings(db, _owner_key(user))
    legs_for_calc = []
    for leg in payload.legs:
        pred = db.query(Prediction).filter_by(match_id=leg.match_id).first()
        if not pred:
            raise HTTPException(404, f"No prediction found for match_id {leg.match_id}")
        prob = {"home": pred.prob_home, "draw": pred.prob_draw, "away": pred.prob_away}[leg.outcome]
        legs_for_calc.append({"prob": prob, "odds": leg.odds, "label": leg.label})

    result = parlay_ev_and_risk(
        legs_for_calc, payload.parlay_odds,
        fraction=settings.kelly_fraction, cap=settings.max_bet_pct,
    )
    result["kelly_amount"] = round(result["kelly_pct"] * settings.bankroll_total, 2)
    return result


class ParlaySuggestMatchInput(BaseModel):
    match_id: int
    odds_home: Optional[float] = None
    odds_draw: Optional[float] = None
    odds_away: Optional[float] = None


class ParlaySuggestInput(BaseModel):
    matches: list[ParlaySuggestMatchInput]
    min_legs: int = 3
    max_legs: int = 6


@app.post("/api/parlay/suggest")
def suggest_parlay_combinations(payload: ParlaySuggestInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """
    Auto-search entry point: give it a pool of matches with your odds
    (only 1X2 — home/draw/away, no other markets), it looks up this
    system's own model probability for each outcome (Dixon-Coles +
    current Bayesian posterior, the same numbers driving the rest of
    the app), filters out every outcome that isn't positive-EV, and
    searches all min_legs-to-max_legs combinations from what's left for
    the best positive-EV parlays.

    Negative-EV legs never enter the candidate pool, by design — this
    directly addresses the intuition that stringing together short-odds
    favorites raises the payout: it does raise the combined odds, but for
    independent events EV_combo = Π(1+EV_i) - 1, so any leg with EV_i<0
    (a common outcome for heavy favorites, who are frequently overpriced
    by the market relative to their true win rate — the well-documented
    "favorite-longshot bias") drags the whole combination down rather
    than helping it, regardless of how safe that leg feels.
    """
    if payload.min_legs < 2:
        raise HTTPException(400, "min_legs must be at least 2")
    if payload.max_legs < payload.min_legs:
        raise HTTPException(400, "max_legs must be >= min_legs")
    if payload.max_legs > 8:
        raise HTTPException(400, "max_legs capped at 8 to keep the combination search fast")

    settings = _get_or_create_settings(db, _owner_key(user))

    # 一次把用到的比赛和预测全查出来，不要在循环里一场一场查。
    # 原来是每场 2 次查询（Match + Prediction）——本地 SQLite 是进程内调用
    # 看不出来，换成云端远端 Postgres，每次往返几十毫秒，选 60 场就是 121 次
    # 往返、纯等网络好几秒。这跟 /api/matches 当初踩的是同一个 N+1，那边
    # 已经改成批量了，这边漏掉了。
    ids = [m.match_id for m in payload.matches]
    matches_by_id = {x.id: x for x in db.query(Match).filter(Match.id.in_(ids)).all()}
    preds_by_id = {p.match_id: p for p in
                   db.query(Prediction).filter(Prediction.match_id.in_(ids)).all()}

    match_odds_list = []
    for m in payload.matches:
        match = matches_by_id.get(m.match_id)
        pred = preds_by_id.get(m.match_id)
        if not match or not pred:
            continue  # skip silently — a match the user picked but that has no prediction yet
        match_odds_list.append({
            "match_id": m.match_id,
            "team1": match.team1, "team2": match.team2,
            "prob_home": pred.prob_home, "prob_draw": pred.prob_draw, "prob_away": pred.prob_away,
            "odds_home": m.odds_home, "odds_draw": m.odds_draw, "odds_away": m.odds_away,
        })

    if not match_odds_list:
        raise HTTPException(404, "None of the submitted match_id values have a prediction on record")

    result = suggest_parlays(
        match_odds_list,
        min_legs=payload.min_legs, max_legs=payload.max_legs,
        fraction=settings.kelly_fraction, cap=settings.max_bet_pct,
        top_n=5,
    )

    for combo in result.get("combinations", []):
        combo["kelly_amount"] = round(combo["kelly_pct"] * settings.bankroll_total, 2)

    return result


class ParlayLegRecord(BaseModel):
    match_id: int
    outcome: str
    odds: float
    prob: Optional[float] = None


class ParlayBetRecord(BaseModel):
    kind: str = "virtual"                    # 'virtual' | 'real'
    legs: list[ParlayLegRecord]
    stake: float
    odds_used: Optional[float] = None        # 不填就用各腿赔率相乘
    joint_probability: Optional[float] = None
    ev_at_bet: Optional[float] = None
    kelly_pct: Optional[float] = None
    platform: str = "bk8"
    currency: str = "HKD"


@app.post("/api/parlay-bets")
def create_parlay_bet(payload: ParlayBetRecord, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """
    把一注串关记进账。虚拟盘和实盘用 kind 区分，两者都会进入资金曲线
    （资金池全局共用，不按赛事分开）。

    odds_used 允许覆盖：博彩公司的串关定价不一定严格等于各腿赔率相乘，
    实盘记录时应该填你真实拿到的总赔率，否则账面盈亏会跟实际对不上。
    """
    if payload.kind not in ("virtual", "real"):
        raise HTTPException(400, "kind must be 'virtual' or 'real'")
    if len(payload.legs) < 2:
        raise HTTPException(400, "串关至少需要2条腿")

    match_ids = [l.match_id for l in payload.legs]
    if len(set(match_ids)) != len(match_ids):
        raise HTTPException(400, "同一场比赛不能在一注串关里出现两次（那不是独立事件）")

    for l in payload.legs:
        if not db.query(Match).filter_by(id=l.match_id).first():
            raise HTTPException(404, f"match_id {l.match_id} 不存在")

    odds_used = payload.odds_used
    if odds_used is None:
        odds_used = 1.0
        for l in payload.legs:
            odds_used *= l.odds

    parlay = ParlayBet(
        owner_id=_owner_key(user),
        kind=payload.kind, stake=payload.stake, odds_used=round(odds_used, 4),
        joint_probability=payload.joint_probability, ev_at_bet=payload.ev_at_bet,
        kelly_pct=payload.kelly_pct, platform=payload.platform,
        currency=payload.currency, result="pending",
    )
    db.add(parlay)
    db.flush()

    for l in payload.legs:
        db.add(ParlayLeg(
            parlay_bet_id=parlay.id, match_id=l.match_id,
            outcome=l.outcome, leg_odds=l.odds, leg_prob=l.prob,
        ))
    db.commit()
    db.refresh(parlay)
    return _parlay_dict(parlay)


def _parlay_dict(p: ParlayBet):
    return {
        "id": p.id, "kind": p.kind, "stake": p.stake, "odds_used": p.odds_used,
        "joint_probability": p.joint_probability, "ev_at_bet": p.ev_at_bet,
        "kelly_pct": p.kelly_pct, "platform": p.platform, "currency": p.currency,
        "result": p.result, "pnl": p.pnl,
        "created_at": p.created_at.isoformat(),
        "settled_at": p.settled_at.isoformat() if p.settled_at else None,
        "legs": [{
            "match_id": l.match_id, "outcome": l.outcome,
            "odds": l.leg_odds, "prob": l.leg_prob,
            # 前端顶部总览栏和实盘页都有联赛筛选。单场注单本身带
            # competition_id 能直接筛，串关此前没有任何联赛信息，结果是
            # 筛到某个联赛时，完全不属于该联赛的串关也被算进注数和金额里。
            # 串关跨联赛是常态，所以联赛挂在腿上而不是整注上。
            "competition_id": l.match.competition_id if l.match else None,
            "team1": l.match.team1 if l.match else None,
            "team2": l.match.team2 if l.match else None,
            "date": l.match.date.isoformat() if l.match else None,
            "score": f"{l.match.score1}-{l.match.score2}" if l.match and l.match.score1 is not None else None,
        } for l in p.legs],
    }


@app.get("/api/parlay-bets")
def list_parlay_bets(kind: Optional[str] = None, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    q = _owned(db.query(ParlayBet), ParlayBet, _owner_key(user))
    if kind:
        q = q.filter(ParlayBet.kind == kind)
    return [_parlay_dict(p) for p in q.order_by(ParlayBet.created_at.desc()).all()]


@app.delete("/api/parlay-bets/{parlay_id}")
def delete_parlay_bet(parlay_id: int, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    """取消一注还没结算的串关。

    这里原来没有 pending 检查——能把已经赢了/输了的串关也删掉，
    那样 /api/bankroll-summary 里已经算进资金曲线和胜率的一笔账会凭空
    消失，历史战绩被悄悄改写而界面上完全看不出发生过什么。删除操作本身
    没留任何痕迹，事后没法查是谁、什么时候删的。加上跟 cancel_bet /
    cancel_real_bet 一致的限制：只能取消还没结算的。
    """
    p = db.query(ParlayBet).filter_by(id=parlay_id).first()
    if not p or not _is_owned(p, _owner_key(user)):
        raise HTTPException(404, "串关注单不存在")
    if p.result != "pending":
        raise HTTPException(400, "已结算的串关不能取消，那会悄悄改掉历史战绩和资金曲线")
    db.delete(p)
    db.commit()
    return {"status": "cancelled", "id": parlay_id}


@app.get("/api/score-distribution/{match_id}")
def get_score_distribution(match_id: int, goals_threshold: float = 2.5, db: Session = Depends(get_db)):
    """
    Same-match joint distribution — e.g. "Spain wins" AND "under 1.5 Spain
    goals" in one match are NOT independent events (they're two views of
    the same underlying Poisson process), so they can't go through
    /api/parlay's probability-multiplication shortcut. This endpoint
    returns the actual joint distribution the Dixon-Coles model already
    computes internally, so questions like "P(team1 scores under X)" get
    answered from the real joint distribution rather than a wrong
    independence assumption.
    """
    from .model import score_distribution, BayesianTeamState

    match = db.query(Match).filter_by(id=match_id).first()
    if not match:
        raise HTTPException(404, "Match not found")

    attack_override, defense_override = {}, {}
    for team_name in (match.team1, match.team2):
        row = db.query(BayesianTeamStateRow).filter_by(
            team_name=team_name, competition_id=match.competition_id
        ).first()
        if row:
            state = BayesianTeamState.from_dict({
                "team_name": row.team_name,
                "attack_shape": row.attack_shape, "attack_rate": row.attack_rate,
                "defense_theta_shape": row.defense_theta_shape, "defense_theta_rate": row.defense_theta_rate,
                "decay": row.decay, "n_updates": row.n_updates,
            })
            attack_override[team_name] = state.current_attack()
            defense_override[team_name] = state.current_defense()

    dist = score_distribution(match.team1, match.team2, attack_override=attack_override, defense_override=defense_override)
    return {
        "match_id": match_id, "team1": match.team1, "team2": match.team2,
        "score_probs": dist["score_probs"],
        "total_goals_probs": dist["total_goals_probs"],
        "team1_goals_under_threshold": dist["team1_goals_under"].get(goals_threshold),
        "team1_goals_over_threshold": dist["team1_goals_over"].get(goals_threshold),
    }


# ══════════════════════════════════════════════════════════
# 价格策略（热门-冷门偏差）
# ══════════════════════════════════════════════════════════
# 这一段跟上面所有基于模型的端点是**两套独立的东西**。
# handoff/09 用 141,287 场证明模型对市场价格的增量信息为零，所以
# 这里的判断完全不看模型概率，只看赔率本身落在哪个档、以及你拿到的价有多好。
# 详见 app/ev_evidence.py 的模块注释。

class StrategyInput(BaseModel):
    odds: float                                  # 你实际能下到的赔率
    market_avg: Optional[float] = None           # 市场平均赔率
    market_best: Optional[float] = None          # 全市场最高赔率


@app.post("/api/strategy/evaluate")
def strategy_evaluate(payload: StrategyInput, db: Session = Depends(get_db)):
    """单注评估：这个赔率在偏差的哪一侧，你的价够不够格。"""
    adv = bet_advisory(payload.odds, payload.market_avg, payload.market_best)
    return {
        **adv,
        "reality_check": reality_check(),
        "capture_table": {str(k): v for k, v in CAPTURE_BY_LEGS.items()},
    }


class ThreeOddsInput(BaseModel):
    """只需要你自己平台的三个赔率，不需要市场行情。"""
    odds_home: float
    odds_draw: float
    odds_away: float
    pick: Optional[str] = None          # 'home' | 'draw' | 'away'，不传则自动选热门侧


@app.post("/api/strategy/evaluate-simple")
def strategy_evaluate_simple(payload: ThreeOddsInput):
    """推荐入口：从三个赔率算出该平台抽水，再判断这一注值不值得下。

    /strategy/evaluate 要填「市场平均价」和「全市场最高价」才能算价格捕获率，
    但多数人手上只有自己那家平台的报价。同一批实测数据换个坐标表达就绕开了
    这个问题——抽水从三个赔率直接算得出来，而抽水才是真正吃掉优势的量
    （捕获率只是它的代理）。
    """
    adv = advisory_from_three_odds(
        payload.odds_home, payload.odds_draw, payload.odds_away, payload.pick
    )
    return {**adv, "reality_check": reality_check()}


class StrategyParlayInput(BaseModel):
    leg_edges: list[float]                       # 各腿的单注预期 ROI
    margin_per_leg: float = TYPICAL_PARLAY_MARGIN_PER_LEG


@app.post("/api/strategy/parlay")
def strategy_parlay(payload: StrategyParlayInput):
    """串关评估：各腿优势复合 vs 平台串关抽水，谁大。"""
    return parlay_advisory(payload.leg_edges, payload.margin_per_leg)


class PriceLogInput(BaseModel):
    match_desc: Optional[str] = None
    platform: str = "bk8"
    selection: Optional[str] = None
    my_odds: float
    market_avg: float
    market_best: float
    note: Optional[str] = None


@app.post("/api/price-log")
def create_price_log(payload: PriceLogInput, db: Session = Depends(get_db)):
    f = price_capture(payload.my_odds, payload.market_avg, payload.market_best)
    row = PriceLog(
        match_desc=payload.match_desc, platform=payload.platform,
        selection=payload.selection, my_odds=payload.my_odds,
        market_avg=payload.market_avg, market_best=payload.market_best,
        capture=f, note=payload.note,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "capture": f}


@app.get("/api/price-log")
def list_price_log(db: Session = Depends(get_db)):
    """返回全部观测，外加你这个平台的价格捕获率汇总。

    汇总是这套东西的核心输出：f 决定策略是正是负，而 f 只能靠实测累积。
    样本太少时明确说「还不够」，不给一个会被当真的数字。
    """
    rows = db.query(PriceLog).order_by(desc(PriceLog.logged_at)).all()
    vals = [r.capture for r in rows if r.capture is not None]

    summary = {"n": len(vals), "mean_capture": None, "se": None,
               "verdict": "样本不足", "expected_roi": None, "enough": False}
    if vals:
        import statistics
        m = statistics.fmean(vals)
        summary["mean_capture"] = round(m, 3)
        if len(vals) >= 2:
            sd = statistics.stdev(vals)
            summary["se"] = round(sd / (len(vals) ** 0.5), 3)
        # 20 条以下不下结论：f 的逐场波动很大，少量样本给出的均值会误导
        if len(vals) >= 20:
            summary["enough"] = True
            summary["expected_roi"] = round(_roi_at_capture(m), 4)
            if m >= 0.6:
                summary["verdict"] = f"捕获率 {m:.0%}——这个平台的价格够用，策略成立"
            elif m <= 0.2:
                summary["verdict"] = f"捕获率 {m:.0%}——价格太差，任何腿数都是亏的，不要做"
            else:
                summary["verdict"] = (f"捕获率 {m:.0%}——落在说不准的区间。"
                                      f"实测噪声在这一带盖过了信号，建议再记 20 条，"
                                      f"或者换个价格更好的平台")
        else:
            summary["verdict"] = f"已记录 {len(vals)} 条，满 20 条才下结论"

    return {
        "rows": [{"id": r.id, "logged_at": r.logged_at.isoformat() if r.logged_at else None,
                  "match_desc": r.match_desc, "platform": r.platform,
                  "selection": r.selection, "my_odds": r.my_odds,
                  "market_avg": r.market_avg, "market_best": r.market_best,
                  "capture": r.capture, "note": r.note} for r in rows],
        "summary": summary,
    }


@app.delete("/api/price-log/{log_id}")
def delete_price_log(log_id: int, db: Session = Depends(get_db)):
    row = db.query(PriceLog).filter_by(id=log_id).first()
    if not row:
        raise HTTPException(404, "Not found")
    db.delete(row)
    db.commit()
    return {"deleted": log_id}


# ══════════════════════════════════════════════════════════
# 托管打包好的前端
# ══════════════════════════════════════════════════════════
# 让后端直接把 frontend/dist 当静态文件发出去，好处是只需要跑一个进程、
# 开一个端口，双击启动脚本就能用，不用同时挂着 uvicorn 和 vite 两个终端。
# 开发时想用 vite 热更新照旧（CORS 已经允许 5173），这里不影响。
_DIST = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")

if os.path.isdir(_DIST):
    app.mount("/assets", StaticFiles(directory=os.path.join(_DIST, "assets")), name="assets")

    @app.get("/")
    def _serve_index():
        # index.html 必须禁用缓存。它是整个应用的入口，里面写死了带内容哈希的
        # JS 文件名（index-CzXrH1SR.js 这种）。浏览器一旦缓存了旧的 index.html，
        # 就会一直去加载旧的 JS——而如果用户是覆盖解压（没删旧文件夹），
        # 旧 JS 还躺在 dist 里，于是新版本装好了却一直显示旧界面，
        # 且看不出任何异常。实际踩过：赛事筛选功能已经发出去了，
        # 手机上能看到，电脑上因为缓存了旧 index.html 而看不到。
        #
        # /assets 下的文件反而可以放心缓存——文件名带内容哈希，
        # 内容变了文件名就变，不存在拿到旧内容的可能。
        return FileResponse(
            os.path.join(_DIST, "index.html"),
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
