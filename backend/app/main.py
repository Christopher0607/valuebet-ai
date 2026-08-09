"""
FastAPI application. Run with:  uvicorn app.main:app --reload --port 8000
See README.md in the project root for full setup instructions.
"""
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
import logging
import os

from .models import (
    init_db, get_db, Match, Prediction, Odds, Bet, RealBet, UserSettings,
    Competition, UpdateLog, BayesianTeamStateRow, ParlayBet, ParlayLeg, PriceLog,
    Withdrawal,
)
from .updater import run_full_update
from .scheduler import start_scheduler, next_run_info
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
)


# 公开路径：不需要登录也能访问。刻意只放这三类——
#   /api/health  探活，部署平台要用
#   /docs /openapi.json  接口文档，不含任何用户数据
#   非 /api 开头的一切（前端静态文件、登录页本身）
_PUBLIC_PREFIXES = ("/api/health",)


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
        "note": "next_scheduled_update is null if this backend process was just restarted — "
                "the schedule only exists while this process is running.",
        "status_note": "last_status 可能是 ok / partial / error；partial 表示有赛事抓取失败，"
                       "失败清单在 last_detail 里。skipped 只会出现在 POST /api/update-now 的"
                       "返回值中（并发跳过时 updater 不写日志行），这里读不到。",
    }


@app.post("/api/update-now")
def update_now(db: Session = Depends(get_db)):
    """Manual trigger — lets you test without waiting 12 hours."""
    return run_full_update(db)


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
def list_competitions(db: Session = Depends(get_db)):
    return [{
        "id": c.id, "code": c.code, "name": c.name, "name_zh": c.name_zh, "is_active": c.is_active,
    } for c in db.query(Competition).all()]


# ══════════════════════════════════════════════════════════
# MATCHES + PREDICTIONS
# ══════════════════════════════════════════════════════════

@app.get("/api/matches")
def list_matches(status_filter: Optional[str] = None, competition_id: Optional[int] = None,
                 db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    q = db.query(Match)
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

    out = []
    for m in matches:
        pred = preds.get(m.id)
        latest_odds = latest_odds_by_match.get(m.id)
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
        })
    return out


def _pred_dict(p: Prediction):
    return {
        "prob_home": p.prob_home, "prob_draw": p.prob_draw, "prob_away": p.prob_away,
        "xg_home": p.xg_home, "xg_away": p.xg_away,
        "attack_home": p.attack_home, "defense_home": p.defense_home,
        "attack_away": p.attack_away, "defense_away": p.defense_away,
        "predicted": p.predicted, "is_correct": p.is_correct, "rps": p.rps,
    }


# ══════════════════════════════════════════════════════════
# ODDS + EV CALCULATION
# ══════════════════════════════════════════════════════════

class OddsInput(BaseModel):
    match_id: int
    odds_home: float
    odds_draw: Optional[float] = None
    odds_away: float


@app.post("/api/odds")
def submit_odds(payload: OddsInput, db: Session = Depends(get_db), user: Optional[dict] = AuthDep):
    match = db.query(Match).filter_by(id=payload.match_id).first()
    if not match:
        raise HTTPException(404, "Match not found")

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
def backtest_summary(competition_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    传 competition_id → 返回该赛事的扁平统计（向后兼容旧格式）
    不传 → 返回按赛事拆分的数组，**不做任何跨赛事聚合**

    为什么不能混着算：不同赛事是不同的球队池、不同的参数表、大概率不同的
    准确率。把世界杯的 67% 和联赛的准确率平均成一个数字，那个数字哪个都
    不代表。加第二个赛事之前先修掉，而不是等数字已经明显错了才发现。
    """
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
        return {
            "competition_id": competition_id,
            "total": total, "correct": correct,
            "accuracy": round(correct / total, 4) if total else 0,
            "avg_rps": round(avg_rps, 4),
            "random_baseline_rps": 0.245,
        }

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

    return {"by_competition": by_competition, "random_baseline_rps": 0.245}


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

    match_odds_list = []
    for m in payload.matches:
        match = db.query(Match).filter_by(id=m.match_id).first()
        pred = db.query(Prediction).filter_by(match_id=m.match_id).first()
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
