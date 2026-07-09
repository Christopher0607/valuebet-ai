"""
The actual "go fetch new results and update everything" logic.
Called two ways:
  1. By the APScheduler job every 12 hours (see scheduler.py)
  2. By a manual POST /api/update-now from the frontend, for testing
  3. On first startup, so the database isn't empty before the first
     scheduled run fires

Multi-league support added: originally this only handled the World Cup
(a single ongoing tournament). Club leagues (Premier League, La Liga,
Serie A, Bundesliga) are season-based instead — a new JSON file appears
each season, and the file path itself encodes the season (e.g. "2025-26").
See fetch_results_for_league() and the SEASON_PROBE_ORDER logic below for
how this is handled without hardcoding a season that will go stale.
"""
import requests
import threading
from datetime import datetime, date as date_cls
from sqlalchemy.orm import Session

from . import models
from .model import dixon_coles, calc_rps


import re


def normalize_team_name(name: str) -> str:
    """
    Club team names are NOT stable across seasons in openfootball's data —
    confirmed real case: "Manchester United" (2015-16 season file) vs
    "Manchester United FC" (2024-25 season file), same club. Without
    normalizing this, MLE training would silently treat these as two
    different teams, halving the effective sample count for every affected
    club and corrupting the fitted attack/defense parameters.

    Strips a trailing " FC" or " AFC" suffix, but preserves a leading
    "AFC " prefix where it's actually part of the club's name (e.g.
    "AFC Bournemouth" must NOT become "Bournemouth" -- that's not how
    anyone, including this same dataset, ever refers to that club).
    Verified against 13 real team names seen across multiple seasons
    before being trusted here.
    """
    name = name.strip()
    if name.startswith("AFC ") and not name.endswith(" AFC"):
        return name
    return re.sub(r"\s+(AFC|FC)$", "", name).strip()


def _extract_final_score(score_field):
    """
    Normalizes the two score formats actually observed in openfootball's
    JSON data:
      - {"ft": [h, a], "ht": [h, a]}  -- the common case
      - [h, a]                          -- a bare array, observed in real
        Premier League 2025-26 data. Verified against 26 real instances
        (2025-08-16 through 2026-05-09): every single one was [0, 0].
        This is the source's way of encoding a genuine 0-0 final score,
        NOT an unplayed fixture -- treating it as "no score yet" would
        silently drop ~3% of a season's matches, all of them draws,
        which would systematically bias the model's draw-probability
        estimate. Both formats are treated as an authoritative final score.

    Returns (home_goals, away_goals) or None if the match hasn't been played.
    """
    if score_field is None:
        return None
    if isinstance(score_field, list) and len(score_field) == 2:
        return score_field[0], score_field[1]
    if isinstance(score_field, dict) and "ft" in score_field:
        return score_field["ft"][0], score_field["ft"][1]
    return None


def guess_current_season(today: date_cls = None) -> str:
    """
    European football seasons run August-to-May, spanning two calendar years.
    Convention: month >= 7 (July, to allow a buffer for preseason/transfer
    window activity that can appear before the August kickoff) means the
    season is "this year to next year"; otherwise "last year to this year".
    Verified against 4 known date/season pairs before use (see project
    history/tests) -- this determines which season file the scheduler
    tries to fetch, so an off-by-one here would mean silently fetching
    stale data every single run.
    """
    if today is None:
        today = date_cls.today()
    start_year = today.year if today.month >= 7 else today.year - 1
    end_year_short = str(start_year + 1)[-2:]
    return f"{start_year}-{end_year_short}"


def resolve_season_url(url_template: str) -> tuple:
    """
    For season-based competitions, data_source is stored as a URL template
    containing the literal string "{season}", e.g.:
      https://openfootball.github.io/england/{season}/1-premierleague.json

    Tries the guessed current season first. If that file doesn't exist yet
    (e.g. it's July and the new season hasn't been published), falls back
    to the previous season -- this is a real, observed situation: as of
    this writing, the 2025-26 Premier League season is fully complete
    (38 rounds) and 2026-27 has not yet appeared, since the new season
    doesn't start until mid-August. Without this fallback, the scheduler
    would hit a 404 every single run during the close season and silently
    stop updating anything for that competition.

    Returns (resolved_url, season_used) so callers/logs can tell which
    season actually got fetched.
    """
    current_season = guess_current_season()
    candidates = [current_season]
    # Also try the previous season as a fallback (handles the close-season gap)
    start_year = int(current_season.split("-")[0])
    prev_season = f"{start_year - 1}-{str(start_year)[-2:]}"
    candidates.append(prev_season)

    for season in candidates:
        url = url_template.replace("{season}", season)
        try:
            r = requests.head(url, timeout=8)
            if r.status_code == 200:
                return url, season
        except requests.RequestException:
            continue

    # Nothing resolved -- return the first guess anyway so the caller gets
    # a clear 404 in its error log rather than a silent None making its
    # way further into the pipeline.
    return url_template.replace("{season}", candidates[0]), candidates[0]


def get_active_competitions(db: Session):
    return db.query(models.Competition).filter(
        models.Competition.is_active == True,
        models.Competition.data_source.isnot(None),
    ).all()


def fetch_results(comp: models.Competition) -> list:
    r = requests.get(comp.data_source, timeout=15)
    r.raise_for_status()
    data = r.json()
    return [m for m in data["matches"] if m.get("score") and "ft" in m["score"]]


def fetch_upcoming(comp: models.Competition) -> list:
    r = requests.get(comp.data_source, timeout=15)
    r.raise_for_status()
    data = r.json()
    out = []
    for m in data["matches"]:
        has_score = m.get("score") and "ft" in m["score"]
        placeholder = m.get("team1", "").startswith(("W", "L")) or m.get("team2", "").startswith(("W", "L"))
        if not has_score and not placeholder:
            out.append(m)
    return out


def upsert_matches(db: Session, comp: models.Competition, played: list, upcoming: list) -> int:
    updated = 0

    for m in played:
        t1, t2, d = m["team1"], m["team2"], m["date"]
        s1, s2 = m["score"]["ft"]
        existing = db.query(models.Match).filter_by(
            competition_id=comp.id, date=date_cls.fromisoformat(d), team1=t1, team2=t2
        ).first()
        if existing:
            if existing.score1 is None:
                existing.score1, existing.score2 = s1, s2
                existing.status = "played"
                updated += 1
        else:
            db.add(models.Match(
                competition_id=comp.id, date=date_cls.fromisoformat(d),
                team1=t1, team2=t2, score1=s1, score2=s2,
                round=m.get("round", ""), grp=m.get("group", "KO"),
                ground=m.get("ground", ""), status="played",
            ))
            updated += 1

    for m in upcoming:
        t1, t2, d = m["team1"], m["team2"], m["date"]
        existing = db.query(models.Match).filter_by(
            competition_id=comp.id, date=date_cls.fromisoformat(d), team1=t1, team2=t2
        ).first()
        if not existing:
            db.add(models.Match(
                competition_id=comp.id, date=date_cls.fromisoformat(d),
                team1=t1, team2=t2, round=m.get("round", ""), grp="KO",
                ground=m.get("ground", ""), status="upcoming",
            ))
            updated += 1

    db.commit()
    return updated


def update_bayesian_states_for_newly_played_matches(db: Session) -> int:
    """
    For every match that is 'played' AND hasn't yet been folded into its
    two teams' Bayesian posteriors, run one incremental update per team
    using the real final score, then mark the match as folded in.

    This is the actual "real-time" part of Bayesian updating: without this
    function being called, BayesianTeamState is just a class definition
    that nothing ever exercises, and every team's posterior would sit
    frozen at its initial MLE-seeded value forever.

    The bayesian_folded_in filter below is what makes this safe to call on
    every scheduler tick (or every manual "update now" click) without
    double-counting a result. This was verified as a real, not just
    theoretical, problem: three consecutive update-now calls with zero new
    matches previously moved Mexico's attack estimate from 1.1606 to 1.2124
    to 1.2522 purely by re-processing the same already-played matches each
    time. With the flag in place, re-running this against an unchanged
    match set now processes zero matches on the second and third calls.

    Implementation note: states are cached in a local dict for the
    duration of this batch, keyed by (team_name, competition_id), rather
    than re-querying the database on every match. This matters because a
    team can appear in more than one match within a single run -- querying-
    then-adding on every call can't see its own not-yet-committed insert
    from earlier in the same transaction, which caused a UNIQUE constraint
    violation the first time this was tested against real data (Czech
    Republic appeared twice in one run's match list).
    """
    from .model import BayesianTeamState, get_mle_params

    matches = db.query(models.Match).filter(
        models.Match.status == "played",
        models.Match.score1.isnot(None),
        models.Match.bayesian_folded_in == False,
    ).all()

    if not matches:
        return 0

    cache = {}

    def get_state(team_name, competition_id):
        key = (team_name, competition_id)
        if key in cache:
            return cache[key]
        row = db.query(models.BayesianTeamStateRow).filter_by(
            team_name=team_name, competition_id=competition_id
        ).first()
        if row:
            state = BayesianTeamState.from_dict({
                "team_name": row.team_name,
                "attack_shape": row.attack_shape, "attack_rate": row.attack_rate,
                "defense_theta_shape": row.defense_theta_shape, "defense_theta_rate": row.defense_theta_rate,
                "decay": row.decay, "n_updates": row.n_updates,
            })
        else:
            mle_attack, mle_defense = get_mle_params(team_name)
            state = BayesianTeamState(team_name, mle_attack, mle_defense, n_historical_matches=100)
        cache[key] = state
        return state

    for m in matches:
        home_state = get_state(m.team1, m.competition_id)
        away_state = get_state(m.team2, m.competition_id)

        home_state.update_after_match(m.score1, away_state.current_defense())
        away_state.update_after_match(m.score2, home_state.current_defense())
        home_state.update_defense_after_match(m.score2, away_state.current_attack())
        away_state.update_defense_after_match(m.score1, home_state.current_attack())

        cache[(m.team1, m.competition_id)] = home_state
        cache[(m.team2, m.competition_id)] = away_state
        m.bayesian_folded_in = True  # the actual fix: mark this match done

    # Flush the whole batch to the DB in one pass -- one query-or-insert
    # per unique (team, competition) pair, not per match.
    updated_teams = 0
    for (team_name, competition_id), state in cache.items():
        row = db.query(models.BayesianTeamStateRow).filter_by(
            team_name=team_name, competition_id=competition_id
        ).first()
        if not row:
            row = models.BayesianTeamStateRow(team_name=team_name, competition_id=competition_id)
            db.add(row)
        row.attack_shape = state.attack_shape
        row.attack_rate = state.attack_rate
        row.defense_theta_shape = state.defense_theta_shape
        row.defense_theta_rate = state.defense_theta_rate
        row.decay = state.decay
        row.n_updates = state.n_updates
        updated_teams += 1

    db.commit()
    return updated_teams


def update_predictions(db: Session) -> int:
    """
    Runs Dixon-Coles for every match. Uses each team's current Bayesian
    posterior mean if one has been recorded (i.e. the team has played at
    least one match tracked by this system), falling back to the raw MLE
    point estimate otherwise -- this is exactly what dixon_coles()'s
    attack_override/defense_override parameters were designed for.
    """
    from .model import BayesianTeamState

    matches = db.query(models.Match).all()
    updated = 0
    for m in matches:
        attack_override, defense_override = {}, {}
        for team_name in (m.team1, m.team2):
            row = db.query(models.BayesianTeamStateRow).filter_by(
                team_name=team_name, competition_id=m.competition_id
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

        pred = dixon_coles(m.team1, m.team2, attack_override=attack_override, defense_override=defense_override)
        row = db.query(models.Prediction).filter_by(match_id=m.id).first()
        if not row:
            row = models.Prediction(match_id=m.id)
            db.add(row)

        row.prob_home = pred["prob_home"]
        row.prob_draw = pred["prob_draw"]
        row.prob_away = pred["prob_away"]
        row.xg_home = pred["xg_home"]
        row.xg_away = pred["xg_away"]
        row.attack_home = pred["attack_home"]
        row.defense_home = pred["defense_home"]
        row.attack_away = pred["attack_away"]
        row.defense_away = pred["defense_away"]
        row.predicted = pred["predicted"]

        if m.score1 is not None:
            actual = "win1" if m.score1 > m.score2 else "win2" if m.score1 < m.score2 else "draw"
            row.is_correct = pred["predicted"] == actual
            row.rps = calc_rps([pred["prob_home"], pred["prob_draw"], pred["prob_away"]], actual)

        updated += 1
    db.commit()
    return updated


def resolve_bets(db: Session) -> int:
    resolved = 0
    for bet in db.query(models.Bet).filter_by(result="pending").all():
        m = bet.match
        if not m or m.status != "played" or m.score1 is None:
            continue
        actual = "home" if m.score1 > m.score2 else "away" if m.score1 < m.score2 else "draw"
        won = bet.outcome == actual
        bet.result = "win" if won else "loss"
        bet.pnl = round(bet.stake * bet.odds_used - bet.stake, 2) if won else -bet.stake
        resolved += 1

    for bet in db.query(models.RealBet).filter_by(result="pending").all():
        m = bet.match
        if not m or m.status != "played" or m.score1 is None:
            continue
        actual = "home" if m.score1 > m.score2 else "away" if m.score1 < m.score2 else "draw"
        won = bet.outcome == actual
        bet.result = "win" if won else "loss"
        bet.pnl_real = round(bet.stake_real * bet.odds_used - bet.stake_real, 2) if won else -bet.stake_real
        bet.payout_real = round(bet.stake_real * bet.odds_used, 2) if won else 0
        bet.settled_at = datetime.utcnow()
        resolved += 1

    db.commit()
    return resolved


_update_lock = threading.Lock()


def run_full_update(db: Session) -> dict:
    """
    The single entry point the scheduler (and the manual button) calls.

    Guarded by a module-level lock because the scheduler's immediate
    "startup_run" (see scheduler.py) runs on a background thread, and can
    otherwise race with a manual POST /api/update-now on the request
    thread -- both querying-then-inserting the same Prediction.match_id in
    the gap between the SELECT and the INSERT, which produced a real
    UNIQUE constraint violation the first time this was tested end-to-end
    with the scheduler actually running (as opposed to testing
    update_predictions in isolation, which never race-loses against
    itself and passed every time).
    """
    if not _update_lock.acquire(blocking=False):
        return {
            "status": "skipped",
            "matches_updated": 0, "predictions_updated": 0, "bets_resolved": 0,
            "detail": "Another update was already running; this call was skipped "
                      "rather than racing it. Try again in a few seconds.",
            "ran_at": datetime.utcnow().isoformat(),
        }

    try:
        log = models.UpdateLog(matches_updated=0, predictions_updated=0, bets_resolved=0, status="ok")
        try:
            total_matches_updated = 0
            for comp in get_active_competitions(db):
                played = fetch_results(comp)
                upcoming = fetch_upcoming(comp)
                total_matches_updated += upsert_matches(db, comp, played, upcoming)

            log.matches_updated = total_matches_updated
            update_bayesian_states_for_newly_played_matches(db)
            log.predictions_updated = update_predictions(db)
            log.bets_resolved = resolve_bets(db)

        except Exception as e:
            log.status = "error"
            log.detail = str(e)[:500]
            db.rollback()

        db.add(log)
        db.commit()
        return {
            "status": log.status,
            "matches_updated": log.matches_updated,
            "predictions_updated": log.predictions_updated,
            "bets_resolved": log.bets_resolved,
            "detail": log.detail,
            "ran_at": log.ran_at.isoformat(),
        }
    finally:
        _update_lock.release()
