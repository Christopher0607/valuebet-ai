"""
FastAPI application. Run with:  uvicorn app.main:app --reload --port 8000
See README.md in the project root for full setup instructions.
"""
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime
from pydantic import BaseModel
from typing import Optional
import logging

from .models import (
    init_db, get_db, Match, Prediction, Odds, Bet, RealBet, UserSettings,
    Competition, UpdateLog, BayesianTeamStateRow,
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
    from .models import SessionLocal
    db = SessionLocal()
    try:
        if not db.query(Competition).filter_by(code="wc2026").first():
            db.add(Competition(
                code="wc2026", name="2026 FIFA World Cup", name_zh="2026世界杯",
                data_source="https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json",
                is_active=True,
            ))
        if not db.query(Competition).filter_by(code="ucl2627").first():
            db.add(Competition(
                code="ucl2627", name="UEFA Champions League 2026/27", name_zh="欧冠 2026/27",
                data_source=None, is_active=False,
            ))
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
    settings = db.query(UserSettings).filter_by(setting_key="default").first()
    base = settings.bankroll_total

    v_bets = db.query(Bet).filter(Bet.result != "pending").order_by(Bet.created_at).all()
    r_bets = db.query(RealBet).filter(RealBet.result != "pending").order_by(RealBet.placed_at).all()

    def build_series(bets, pnl_attr, stake_attr):
        running = base
        points = [{"date": datetime.utcnow().date().isoformat(), "balance": running}]
        for b in bets:
            running += getattr(b, pnl_attr) or 0
            ts = getattr(b, "created_at", None) or getattr(b, "placed_at", None)
            points.append({"date": ts.date().isoformat(), "balance": round(running, 2)})
        return points

    v_series = build_series(v_bets, "pnl", "stake")
    r_series = build_series(r_bets, "pnl_real", "stake_real")

    v_pnl = sum(b.pnl or 0 for b in v_bets)
    r_pnl = sum(b.pnl_real or 0 for b in r_bets)
    v_staked = sum(b.stake for b in v_bets) or 1
    r_staked = sum(b.stake_real for b in r_bets) or 1

    return {
        "bankroll_base": base,
        "virtual": {
            "series": v_series, "total_pnl": round(v_pnl, 2),
            "roi_pct": round(v_pnl / v_staked * 100, 2),
            "total_bets": len(v_bets), "wins": sum(1 for b in v_bets if b.result == "win"),
        },
        "real": {
            "series": r_series, "total_pnl": round(r_pnl, 2),
            "roi_pct": round(r_pnl / r_staked * 100, 2),
            "total_bets": len(r_bets), "wins": sum(1 for b in r_bets if b.result == "win"),
        },
    }


# ══════════════════════════════════════════════════════════
# BACKTEST SUMMARY
# ══════════════════════════════════════════════════════════

@app.get("/api/backtest-summary")
def backtest_summary(db: Session = Depends(get_db)):
    preds = db.query(Prediction).join(Match).filter(Match.status == "played").all()
    total = len(preds)
    correct = sum(1 for p in preds if p.is_correct)
    avg_rps = sum(p.rps or 0 for p in preds) / total if total else 0
    return {
        "total": total, "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0,
        "avg_rps": round(avg_rps, 4),
        "random_baseline_rps": 0.245,
    }


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
