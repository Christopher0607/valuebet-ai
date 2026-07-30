"""
FastAPI application. Run with:  uvicorn app.main:app --reload --port 8000
See README.md in the project root for full setup instructions.
"""
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
import logging
import os

from .models import (
    init_db, get_db, Match, Prediction, Odds, Bet, RealBet, UserSettings,
    Competition, UpdateLog, BayesianTeamStateRow, ParlayBet, ParlayLeg,
)
from .updater import run_full_update
from .scheduler import start_scheduler, next_run_info
from .model import expected_value, kelly_pct, BayesianTeamState, parlay_ev_and_risk, suggest_parlays

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="ValueBet Local API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev server
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
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
            ("ucl", "UEFA Champions League", "欧冠", f"{BASE}/uefa.cl.json", True),
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
    from .models import SessionLocal
    db = SessionLocal()
    try:
        if not db.query(UserSettings).filter_by(setting_key="default").first():
            db.add(UserSettings(setting_key="default"))
            db.commit()
    finally:
        db.close()


# ══════════════════════════════════════════════════════════
# STATUS
# ══════════════════════════════════════════════════════════

@app.get("/api/status")
def status(db: Session = Depends(get_db)):
    last_run = db.query(UpdateLog).order_by(desc(UpdateLog.ran_at)).first()
    return {
        "last_update": last_run.ran_at.isoformat() if last_run else None,
        "last_status": last_run.status if last_run else None,
        "last_detail": last_run.detail if last_run else None,
        "next_scheduled_update": next_run_info(),
        "note": "next_scheduled_update is null if this backend process was just restarted — "
                "the schedule only exists while this process is running.",
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
def list_matches(status_filter: Optional[str] = None, competition_id: Optional[int] = None, db: Session = Depends(get_db)):
    q = db.query(Match)
    if status_filter:
        q = q.filter(Match.status == status_filter)
    if competition_id:
        q = q.filter(Match.competition_id == competition_id)
    matches = q.order_by(Match.date).all()

    out = []
    for m in matches:
        pred = db.query(Prediction).filter_by(match_id=m.id).first()
        latest_odds = db.query(Odds).filter_by(match_id=m.id).order_by(desc(Odds.recorded_at)).first()
        out.append({
            "id": m.id, "competition_id": m.competition_id,
            "date": m.date.isoformat(), "team1": m.team1, "team2": m.team2,
            "score1": m.score1, "score2": m.score2,
            "round": m.round, "grp": m.grp, "ground": m.ground, "status": m.status,
            "prediction": _pred_dict(pred) if pred else None,
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
def submit_odds(payload: OddsInput, db: Session = Depends(get_db)):
    match = db.query(Match).filter_by(id=payload.match_id).first()
    if not match:
        raise HTTPException(404, "Match not found")

    db.add(Odds(
        match_id=payload.match_id, source="manual",
        odds_home=payload.odds_home, odds_draw=payload.odds_draw, odds_away=payload.odds_away,
    ))
    db.commit()

    pred = db.query(Prediction).filter_by(match_id=payload.match_id).first()
    if not pred:
        raise HTTPException(400, "No prediction available for this match yet")

    settings = db.query(UserSettings).filter_by(setting_key="default").first()
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
def list_bets(db: Session = Depends(get_db)):
    bets = db.query(Bet).order_by(desc(Bet.created_at)).all()
    return [_bet_dict(b) for b in bets]


def _bet_dict(b: Bet):
    m = b.match
    return {
        "id": b.id, "match_id": b.match_id,
        "team1": m.team1 if m else None, "team2": m.team2 if m else None, "date": m.date.isoformat() if m else None,
        "outcome": b.outcome, "stake": b.stake, "odds_used": b.odds_used,
        "ev_at_bet": b.ev_at_bet, "kelly_pct": b.kelly_pct, "result": b.result, "pnl": b.pnl,
        "created_at": b.created_at.isoformat(),
    }


@app.post("/api/bets")
def create_bet(payload: BetInput, db: Session = Depends(get_db)):
    bet = Bet(**payload.dict(), result="pending")
    db.add(bet)
    db.commit()
    db.refresh(bet)
    return _bet_dict(bet)


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
def list_real_bets(db: Session = Depends(get_db)):
    bets = db.query(RealBet).order_by(desc(RealBet.placed_at)).all()
    return [_real_bet_dict(b) for b in bets]


def _real_bet_dict(b: RealBet):
    m = b.match
    return {
        "id": b.id, "match_id": b.match_id,
        "team1": m.team1 if m else None, "team2": m.team2 if m else None, "date": m.date.isoformat() if m else None,
        "platform": b.platform, "outcome": b.outcome, "stake_real": b.stake_real, "currency": b.currency,
        "odds_used": b.odds_used, "ev_at_bet": b.ev_at_bet,
        "kelly_suggested_amount": b.kelly_suggested_amount,
        "result": b.result, "pnl_real": b.pnl_real, "payout_real": b.payout_real,
        "placed_at": b.placed_at.isoformat(),
        "settled_at": b.settled_at.isoformat() if b.settled_at else None,
    }


@app.post("/api/real-bets")
def create_real_bet(payload: RealBetInput, db: Session = Depends(get_db)):
    data = payload.dict()
    kelly_amt = data.get("kelly_suggested_amount")
    followed = None
    if kelly_amt:
        followed = abs(data["stake_real"] - kelly_amt) < kelly_amt * 0.15
    bet = RealBet(**data, actually_followed_kelly=followed, result="pending")
    db.add(bet)
    db.commit()
    db.refresh(bet)
    return _real_bet_dict(bet)


# ══════════════════════════════════════════════════════════
# SETTINGS (custom bankroll / Kelly fraction / caps)
# ══════════════════════════════════════════════════════════

class SettingsInput(BaseModel):
    bankroll_total: float
    kelly_fraction: float
    max_bet_pct: float
    min_ev_threshold: float


@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    s = db.query(UserSettings).filter_by(setting_key="default").first()
    return {
        "bankroll_total": s.bankroll_total, "kelly_fraction": s.kelly_fraction,
        "max_bet_pct": s.max_bet_pct, "min_ev_threshold": s.min_ev_threshold,
    }


@app.put("/api/settings")
def update_settings(payload: SettingsInput, db: Session = Depends(get_db)):
    s = db.query(UserSettings).filter_by(setting_key="default").first()
    for k, v in payload.dict().items():
        setattr(s, k, v)
    db.commit()
    return {"status": "saved"}


# ══════════════════════════════════════════════════════════
# BANKROLL SUMMARY (for the chart)
# ══════════════════════════════════════════════════════════

@app.get("/api/bankroll-summary")
def bankroll_summary(db: Session = Depends(get_db)):
    """
    资金走势 + 汇总。资金池是全局共用的（所有赛事共享一个 bankroll_total），
    盈亏统计按虚拟盘/实盘分开，但不按赛事分开——赛事维度的统计在
    /api/backtest-summary，那个是模型预测准确率，跟资金流向是两回事。

    串关盈亏计入对应的虚拟/实盘曲线（一注串关算一个资金事件）。
    """
    settings = db.query(UserSettings).filter_by(setting_key="default").first()
    base = settings.bankroll_total

    v_bets = db.query(Bet).filter(Bet.result != "pending").all()
    r_bets = db.query(RealBet).filter(RealBet.result != "pending").all()
    parlays = db.query(ParlayBet).filter(ParlayBet.result != "pending").all()

    # 把所有已结算的资金事件收敛成 (日期, 盈亏, 虚拟还是实盘) 三元组
    events = []
    for b in v_bets:
        events.append((b.created_at.date(), b.pnl or 0, "virtual"))
    for b in r_bets:
        events.append((b.placed_at.date(), b.pnl_real or 0, "real"))
    for p in parlays:
        events.append((p.created_at.date(), p.pnl or 0, "virtual" if p.kind == "virtual" else "real"))

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
        },
    }


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
def calculate_parlay(payload: ParlayInput, db: Session = Depends(get_db)):
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

    settings = db.query(UserSettings).filter_by(setting_key="default").first()
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
def suggest_parlay_combinations(payload: ParlaySuggestInput, db: Session = Depends(get_db)):
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

    settings = db.query(UserSettings).filter_by(setting_key="default").first()

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
def create_parlay_bet(payload: ParlayBetRecord, db: Session = Depends(get_db)):
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
            "team1": l.match.team1 if l.match else None,
            "team2": l.match.team2 if l.match else None,
            "date": l.match.date.isoformat() if l.match else None,
            "score": f"{l.match.score1}-{l.match.score2}" if l.match and l.match.score1 is not None else None,
        } for l in p.legs],
    }


@app.get("/api/parlay-bets")
def list_parlay_bets(kind: Optional[str] = None, db: Session = Depends(get_db)):
    q = db.query(ParlayBet)
    if kind:
        q = q.filter(ParlayBet.kind == kind)
    return [_parlay_dict(p) for p in q.order_by(ParlayBet.created_at.desc()).all()]


@app.delete("/api/parlay-bets/{parlay_id}")
def delete_parlay_bet(parlay_id: int, db: Session = Depends(get_db)):
    p = db.query(ParlayBet).filter_by(id=parlay_id).first()
    if not p:
        raise HTTPException(404, "串关注单不存在")
    db.delete(p)
    db.commit()
    return {"status": "deleted", "id": parlay_id}


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
        return FileResponse(os.path.join(_DIST, "index.html"))
