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
from .model import dixon_coles, calc_rps, scope_for_competition, neutral_for_competition


import re


def normalize_team_name(name: str) -> str:
    """
    Club team names are NOT stable across seasons in openfootball's data —
    confirmed real case: "Manchester United" (2015-16 season file) vs
    "Manchester United FC" (2024-25 season file), same club. Without
    normalizing this, MLE training would silently treat these as two
    different teams, halving the effective sample count for every affected
    club and corrupting the fitted attack/defense parameters.

    Three passes:
    0. Strip a trailing country code like " (ENG)" / " (ITA)". Champions
       League data tags every team this way ("Aston Villa FC (ENG)") while
       domestic league data and the fitted parameter table do not. Without
       this, every UCL team misses the parameter lookup and falls through
       to the meaningless (0,0) fallback — and the cross-league calibration
       that makes UCL predictions possible at all would silently break.
    1. Strip a trailing " FC" or " AFC" suffix, but preserve a leading
       "AFC " prefix where it's actually part of the club's name (e.g.
       "AFC Bournemouth" must NOT become "Bournemouth"). This alone fully
       resolves English Premier League naming drift — verified by cross-
       referencing all 11 historical seasons (2015-16 to 2025-26) against
       the actual current 2025-26 EPL roster: every one of the 14 residual
       unmatched historical names turned out to be a genuinely-relegated
       club (Cardiff City, Watford, etc.), not an unhandled variant.
    2. Look up an explicit alias table for cases the suffix rule can't
       handle — different word order ("Atlético Madrid" vs "Club Atlético
       de Madrid"), prefix additions ("Bayern München" vs "FC Bayern
       München"), or abbreviation expansion ("Bor. Mönchengladbach" vs
       "Borussia Mönchengladbach"). Every entry below was individually
       verified: fetch all 11 historical seasons, fetch the current
       2025-26 roster, diff them, and manually confirm each residual name
       against real club identities rather than guessed via a fuzzier
       automated rule (a shared-substring heuristic risks false-positive
       merges here — e.g. "Real Madrid", "Real Betis", "Real Sociedad",
       "Real Valladolid" all share the word "Real" but are four different
       clubs).
    """
    name = re.sub(r"\s*\([A-Z]{3}\)\s*$", "", (name or "").strip())
    if name.startswith("AFC ") and not name.endswith(" AFC"):
        pass
    else:
        name = re.sub(r"\s+(AFC|FC)$", "", name).strip()

    return _CLUB_NAME_ALIASES.get(name, name)


_CLUB_NAME_ALIASES = {
    # Premier League —— .txt 源仓库里写 "Bournemouth"，.json 镜像和拟合出来的
    # 参数表里都是 "AFC Bournemouth"。上面那条"保留 AFC 前缀"的规则只保证
    # "AFC Bournemouth" 不被削成 "Bournemouth"，反过来这一半它管不到。
    # 目前生产上不会撞到（只有 .json 缺失的新赛季才走 .txt，而新赛季文件
    # 用的正是 "AFC Bournemouth"），但只要哪天回退到旧赛季的 .txt，
    # 这家俱乐部就会被拆成两支球队——正是这个函数存在的理由。
    "Bournemouth": "AFC Bournemouth",
    # La Liga — verified against 2025-26 roster (Real Valladolid used
    # match-count tiebreak: 114 vs 76, since Valladolid isn't in the
    # 2025-26 top flight and there's no current-season signal to check)
    "Atlético Madrid": "Club Atlético de Madrid",
    "CD Alavés": "Deportivo Alavés",
    "RC Celta": "RC Celta de Vigo",
    "Espanyol Barcelona": "RCD Espanyol de Barcelona",
    "Rayo Vallecano": "Rayo Vallecano de Madrid",
    "Real Betis": "Real Betis Balompié",
    "Real Madrid": "Real Madrid CF",
    "Real Sociedad": "Real Sociedad de Fútbol",
    "Real Valladolid": "Real Valladolid CF",
    # Serie A — verified against 2025-26 roster (UC Sampdoria used
    # match-count tiebreak: 190 vs 114, Sampdoria isn't currently Serie A)
    "Atalanta": "Atalanta BC",
    "Bologna": "Bologna FC 1909",
    "Inter": "FC Internazionale Milano",
    "Lazio Roma": "SS Lazio",
    "Sassuolo Calcio": "US Sassuolo Calcio",
    "UC Sampdoria": "Sampdoria",
    # Bundesliga — verified against 2025-26 roster
    "1899 Hoffenheim": "TSG 1899 Hoffenheim",
    "Bayer Leverkusen": "Bayer 04 Leverkusen",
    "Bayern München": "FC Bayern München",
    "Bor. Mönchengladbach": "Borussia Mönchengladbach",
    "Werder Bremen": "SV Werder Bremen",
}


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


# ── 新赛季赛程：.json 镜像落后于 .txt 源仓库 ──────────────────
#
# openfootball 有两套东西：各国的源仓库（england / espana / italy /
# deutschland，赛程写成 .txt），以及由它们生成的 football.json 镜像。
# **镜像的生成是滞后的。** 2026-08-01 实测：
#
#   football.json/2026-27/en.1.json          404
#   england/2026-27/1-premierleague.txt      200  ← 八月开赛的完整赛程在这里
#
# 只认 .json 的话，resolve_season_url 会一路退回 2025-26——那一季已经
# 全部踢完，于是「接下来的比赛」是空的。用户在别处查得到八月的赛程，
# 在自己的系统里查不到，看起来像系统坏了，其实是取错了文件。
#
# 所以按赛季从新到旧逐个试，每个赛季先试 .json 再试 .txt，取第一个存在的。
# 欧冠没有对应的 .txt 源（八月底才抽签，任何地方都还没有赛程），
# 它继续走 .json 那条路，等镜像更新。
_TXT_SOURCES = {
    "en.1": "https://raw.githubusercontent.com/openfootball/england/master/{season}/1-premierleague.txt",
    "es.1": "https://raw.githubusercontent.com/openfootball/espana/master/{season}/1-liga.txt",
    "it.1": "https://raw.githubusercontent.com/openfootball/italy/master/{season}/1-seriea.txt",
    "de.1": "https://raw.githubusercontent.com/openfootball/deutschland/master/{season}/1-bundesliga.txt",
}

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}

# 日期行。已完赛的赛季顶格写、新赛季缩进两格，所以前导空白必须放开。
# 年份只在变化时出现（实测 "Sat Dec 26" 之后是 "Sat Jan 2 2027"）。
_TXT_DATE = re.compile(
    r"^\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:\s+(\d{4}))?\s*$")
# 未开赛："20:00  Arsenal FC              v Coventry City FC"
_TXT_FIXTURE = re.compile(r"^\s+(?:(\d{1,2}):(\d{2})\s+)?(\S.*?)\s+v\s+(\S.*?)\s*$")
# 已完赛："19:00   Liverpool  4-2 (1-0)  Bournemouth"，半场比分可有可无
_TXT_RESULT = re.compile(
    r"^\s+(?:(\d{1,2}):(\d{2})\s+)?(\S.*?)\s+(\d{1,2})-(\d{1,2})(?:\s+\([\d\-]+\))?\s+(\S.*?)\s*$")
_TXT_ROUND = re.compile(r"^\s*[▪»]\s*(.+?)\s*$")


def parse_openfootball_txt(text: str) -> list:
    """把 openfootball 的 .txt 赛程解析成跟 .json 同形的记录。

    产出的字段跟 football.json 对齐（date / team1 / team2 / score / round），
    这样下游的 _extract_final_score、upsert_matches 一行都不用改。

    要处理的坑（都是对着两季真实文件确认的，不是设想）：
      · 同一个联赛，已完赛的赛季和新赛季**缩进不一样**（顶格 vs 缩进两格）
      · 队名在新赛季带 FC 后缀、旧赛季不带，交给 normalize_team_name 抹平
      · 进球者写在括号里、可以跨多行，必须整块跳过。用括号配平来判断，
        不能只看行首是不是 "("——续行是 "Antoine SEMENYO 64', 76')"，
        行首没有括号，但它仍然属于上一行的括号块
      · 没写时间的比赛沿用同一天上一场的时间
    """
    out = []
    year = None
    cur_date = None
    cur_round = None
    paren_depth = 0

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        # 括号块（进球者名单）整块跳过。先处理再做别的判断，否则
        # "(Matt ORILEY 55'(p); RODRIGO MUNIZ 90+7')" 这种行会被误当成比赛。
        if paren_depth > 0:
            paren_depth += line.count("(") - line.count(")")
            continue
        if line.strip().startswith("("):
            paren_depth = line.count("(") - line.count(")")
            continue

        s = line.strip()
        if s.startswith("#") or s.startswith("="):
            continue

        m = _TXT_ROUND.match(line)
        if m:
            cur_round = m.group(1)
            continue

        m = _TXT_DATE.match(line)
        if m:
            mon, day, yr = _MONTHS[m.group(1)], int(m.group(2)), m.group(3)
            if yr:
                year = int(yr)
            elif year is None:
                continue                      # 还没见过任何年份，无从推断
            elif cur_date and mon < cur_date.month:
                year += 1                     # 兜底：12 月跳到 1 月而上游漏写年份
            cur_date = date_cls(year, mon, day)
            continue

        if cur_date is None:
            continue

        m = _TXT_RESULT.match(line)
        if m:
            _h, _mi, t1, sh, sa, t2 = m.groups()
            out.append({"date": cur_date.isoformat(), "round": cur_round,
                        "team1": t1.strip(), "team2": t2.strip(),
                        "score": {"ft": [int(sh), int(sa)]}})
            continue

        m = _TXT_FIXTURE.match(line)
        if m:
            _h, _mi, t1, t2 = m.groups()
            out.append({"date": cur_date.isoformat(), "round": cur_round,
                        "team1": t1.strip(), "team2": t2.strip(), "score": None})

    return out


def get_active_competitions(db: Session):
    return db.query(models.Competition).filter(
        models.Competition.is_active == True,
        models.Competition.data_source.isnot(None),
    ).all()


def _resolve_data_source(comp: models.Competition):
    """挑出这个赛事该用哪个文件，返回 (url, 格式)，格式是 "json" 或 "txt"。

    世界杯的 data_source 是一个固定 URL（一届赛事，没有赛季概念）。
    俱乐部联赛存的是含 "{season}" 占位符的模板。

    按赛季从新到旧逐个试，**每个赛季先试 .json 再试 .txt**，第一个存在的
    就用它。这个顺序是关键：只试 .json 的话，2026 年 8 月会一路退回
    2025-26（已完赛），页面上一场未来赛事都没有——而 .txt 源仓库里
    2026-27 的赛程早就发布了。实测数据见 _TXT_SOURCES 上方的注释。
    """
    if "{season}" not in comp.data_source:
        return comp.data_source, "json"

    # 从 .json 模板里取出 "en.1" 这样的键，用来查对应的 .txt 源
    key = comp.data_source.rstrip("/").rsplit("/", 1)[-1].removesuffix(".json")
    txt_template = _TXT_SOURCES.get(key)

    start_year = int(guess_current_season().split("-")[0])
    for back in range(5):
        y = start_year - back
        season = f"{y}-{str(y + 1)[-2:]}"
        for template, fmt in ((comp.data_source, "json"), (txt_template, "txt")):
            if not template:
                continue
            url = template.replace("{season}", season)
            try:
                if requests.head(url, timeout=8).status_code == 200:
                    return url, fmt
            except requests.RequestException:
                continue

    # 全都找不到：返回最新赛季的 .json，让调用方拿到一个明确的 404，
    # 而不是一个静默的 None 往下游传
    return comp.data_source.replace("{season}", f"{start_year}-{str(start_year + 1)[-2:]}"), "json"


def _fetch_matches(comp: models.Competition) -> list:
    """取回这个赛事的全部比赛记录，统一成 football.json 的形状。

    fetch_results 和 fetch_upcoming 原来各自跑一遍探测阶梯再各自 GET 一次
    ——同一个文件下载两遍，探测也做两遍。合并到这里，两边都只是在这份
    结果上做过滤。
    """
    url, fmt = _resolve_data_source(comp)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    if fmt == "txt":
        # requests 对 text/plain 会按 ISO-8859-1 猜编码，而这些文件是 UTF-8
        # （München、Atlético 都会被毁掉）。显式指定，别让它猜。
        r.encoding = "utf-8"
        return parse_openfootball_txt(r.text)
    return r.json()["matches"]


def fetch_results(comp: models.Competition) -> list:
    return [m for m in _fetch_matches(comp) if _extract_final_score(m.get("score")) is not None]


# 淘汰赛对阵还没确定时，openfootball 用 "Winner Match 73" / "Loser SF1" /
# "1st Group B" 这类占位符当队名，这些不该被当成真实赛程存进库。
#
# 原来的判断是 name.startswith(("W", "L"))，它会误伤**任何以 W 或 L 开头的
# 真实球队**。对着线上文件实测：2025-26 英超 380 场里 140 场（36.8%）被丢掉
# （Liverpool、Leeds、West Ham、Wolverhampton），西甲 38 场，欧冠里
# Liverpool FC (ENG) 和 Lille OSC (FRA) 整季消失。不报错、不记日志、不计数——
# 现在是休赛期没人看得出来，八月赛程一发布就会静默吞掉三分之一的比赛。
#
# 而且它当时什么都没保护到：2026 世界杯文件里以 W/L 开头的队名是零个。
_BRACKET_PLACEHOLDER = re.compile(r"^(Winner|Loser|Runner-up|1st|2nd|3rd)\b", re.I)


def _is_bracket_placeholder(name: str) -> bool:
    return bool(_BRACKET_PLACEHOLDER.match((name or "").strip()))


def fetch_upcoming(comp: models.Competition) -> list:
    out = []
    for m in _fetch_matches(comp):
        has_score = _extract_final_score(m.get("score")) is not None
        placeholder = _is_bracket_placeholder(m.get("team1", "")) or _is_bracket_placeholder(m.get("team2", ""))
        if not has_score and not placeholder:
            out.append(m)
    return out


def upsert_matches(db: Session, comp: models.Competition, played: list, upcoming: list) -> int:
    updated = 0

    # 该赛事已有的比赛一次性读进字典，按 (日期, 主队, 客队) 索引。
    # 原来是每场比赛发一次 filter_by(...).first()，一个赛季 380 场就是
    # 380 次往返，六个赛事加起来近两千次。本地 SQLite 无感，云端远端
    # Postgres 上这一步就要跑几十秒——而它排在生成预测的前面，整轮更新
    # 越接近超时，就越容易在轮到预测之前先被打断。
    existing_by_key = {
        (mm.date, mm.team1, mm.team2): mm
        for mm in db.query(models.Match).filter_by(competition_id=comp.id).all()
    }

    for m in played:
        t1, t2, d = normalize_team_name(m["team1"]), normalize_team_name(m["team2"]), m["date"]
        s1, s2 = _extract_final_score(m["score"])
        existing = existing_by_key.get((date_cls.fromisoformat(d), t1, t2))
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
        t1, t2, d = normalize_team_name(m["team1"]), normalize_team_name(m["team2"]), m["date"]
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

    # 赛事id → 赛事code → 参数作用域。贝叶斯的先验种子来自 MLE 点估计，
    # 取错表的话俱乐部球队会拿到国家队表的兜底值(0,0)当种子，后验从一开始就是错的。
    comp_scope = {}
    for c in db.query(models.Competition).all():
        comp_scope[c.id] = scope_for_competition(c.code)

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
            scope = comp_scope.get(competition_id, "international")
            mle_attack, mle_defense = get_mle_params(team_name, scope)
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
    # 一次性查出赛事表，循环里直接查字典，不要每场比赛都打一次数据库
    comp_by_id = {c.id: c for c in db.query(models.Competition).all()}

    # 贝叶斯状态和已有预测也必须一次性取出来。原来是在循环里按 (队名, 赛事)
    # 查两次贝叶斯状态、再查一次 Prediction —— 每场 3 次，三千多场就是
    # 一万次往返。本地 SQLite 是进程内调用感觉不到，云端远端 Postgres 上
    # 这个函数要跑好几分钟。
    #
    # 而下面只在**全部跑完之后**才 commit 一次。也就是说中途被超时掐断、
    # 或者连接池把连接回收掉，这一万次工作全部回滚，一条预测都不落库——
    # 而 upsert_matches 是单独提交的，比赛却已经存进去了。表现就是：
    # 串关页列得出比赛（它不需要预测），单场预测页一张卡都不渲染
    # （MatchCard 在 match.prediction 为空时直接 return null）。
    bayes_by_key = {
        (r.team_name, r.competition_id): r
        for r in db.query(models.BayesianTeamStateRow).all()
    }
    pred_by_match = {p.match_id: p for p in db.query(models.Prediction).all()}

    # commit() 默认会让 session 里所有已加载对象过期，下一次访问 m.team1
    # 就得为那一行再发一次 SELECT。配合下面的分批提交，等于每提交一次就
    # 把剩下的 Match 逐行重读一遍——分批提交本身又造出一个 N+1。
    # 实测：1739 场比赛，开着它是 3024 次查询，关掉是 1745 次。
    # 这里提交后不再依赖任何对象的新鲜度（循环只读 m 的固有字段），
    # 所以关掉是安全的；退出时恢复原值，不影响调用方的 session。
    prev_expire = db.expire_on_commit
    db.expire_on_commit = False
    try:
        return _update_predictions_inner(db, matches, comp_by_id, bayes_by_key,
                                         pred_by_match, BayesianTeamState)
    finally:
        db.expire_on_commit = prev_expire


def _update_predictions_inner(db, matches, comp_by_id, bayes_by_key,
                              pred_by_match, BayesianTeamState) -> int:
    updated = 0
    for m in matches:
        attack_override, defense_override = {}, {}
        for team_name in (m.team1, m.team2):
            row = bayes_by_key.get((team_name, m.competition_id))
            if row:
                state = BayesianTeamState.from_dict({
                    "team_name": row.team_name,
                    "attack_shape": row.attack_shape, "attack_rate": row.attack_rate,
                    "defense_theta_shape": row.defense_theta_shape, "defense_theta_rate": row.defense_theta_rate,
                    "decay": row.decay, "n_updates": row.n_updates,
                })
                attack_override[team_name] = state.current_attack()
                defense_override[team_name] = state.current_defense()

        comp = comp_by_id.get(m.competition_id)
        code = comp.code if comp else ""
        pred = dixon_coles(
            m.team1, m.team2,
            attack_override=attack_override, defense_override=defense_override,
            scope=scope_for_competition(code),
            neutral=neutral_for_competition(code),
        )
        row = pred_by_match.get(m.id)
        if not row:
            row = models.Prediction(match_id=m.id)
            db.add(row)
            pred_by_match[m.id] = row

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
        # 分批提交，而不是全部跑完才提交一次。中途出问题时已经算好的部分
        # 能留下来，下一轮更新接着补剩下的；一次性提交则是全有或全无，
        # 而"全无"在界面上表现为预测页整页空白，看起来像功能坏了。
        if updated % 500 == 0:
            db.commit()

    db.commit()
    return updated


def resolve_parlay_bets(db: Session) -> int:
    """
    结算串关注单。

    结算规则（跟单场注单不同，容易写错，这里说清楚）：
    - 任何一腿输了 → 整注立刻判负，**不需要等其余场次踢完**。一注5腿的串关
      如果第1场就输了，剩下4场结果如何都不影响，立刻结算为 loss。
    - 全部腿都踢完且全中 → 判赢。
    - 没有腿输、但还有腿没踢 → 保持 pending。

    如果按单场那种「所有关联比赛都踢完才结算」的写法，已经确定输掉的串关会
    在账面上挂着 pending 好几天，资金曲线就是错的。
    """
    resolved = 0
    pending = db.query(models.ParlayBet).filter_by(result="pending").all()

    for parlay in pending:
        any_leg_lost = False
        all_legs_played = True

        for leg in parlay.legs:
            m = leg.match
            if not m or m.status != "played" or m.score1 is None:
                all_legs_played = False
                continue
            actual = "home" if m.score1 > m.score2 else "away" if m.score1 < m.score2 else "draw"
            if leg.outcome != actual:
                any_leg_lost = True
                break  # 一腿输了就够了，不用再看其他腿

        if any_leg_lost:
            parlay.result = "loss"
            parlay.pnl = -parlay.stake
            parlay.settled_at = datetime.utcnow()
            resolved += 1
        elif all_legs_played:
            parlay.result = "win"
            parlay.pnl = round(parlay.stake * parlay.odds_used - parlay.stake, 2)
            parlay.settled_at = datetime.utcnow()
            resolved += 1
        # 否则保持 pending

    db.commit()
    return resolved


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
            failed_comps = []
            for comp in get_active_competitions(db):
                # 每个赛事单独 try —— 一个赛事的数据源挂了（比如某季文件还没
                # 发布），不该连累其他赛事。实测踩过：欧冠 404 导致整个事务回滚，
                # 另外5个赛事的预测一条都没生成。
                try:
                    played = fetch_results(comp)
                    upcoming = fetch_upcoming(comp)
                    total_matches_updated += upsert_matches(db, comp, played, upcoming)
                except Exception as ce:
                    db.rollback()
                    failed_comps.append(f"{comp.code}: {str(ce)[:120]}")

            log.matches_updated = total_matches_updated
            update_bayesian_states_for_newly_played_matches(db)
            log.predictions_updated = update_predictions(db)
            log.bets_resolved = resolve_bets(db) + resolve_parlay_bets(db)
            if failed_comps:
                # 部分失败照样算这次更新跑完了，但把失败的赛事记下来，
                # 否则数据悄悄少了一个赛事却看不出来
                log.status = "partial"
                log.detail = "以下赛事抓取失败（其余正常）: " + "; ".join(failed_comps)

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
