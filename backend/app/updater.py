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
import logging
from datetime import datetime, date as date_cls
from sqlalchemy.orm import Session

from . import models
from .model import dixon_coles, calc_rps, scope_for_competition, neutral_for_competition
from . import api_football, odds_api

# 走 API-Football + The Odds API 这条完全独立路径的赛事代码——历史比分从
# API-Football 拿（见 api_football.py 顶部注释：免费档只开放 2022-2024），
# 未来赛程从 The Odds API 拿（不受那个赛季限制，但也不含任何赔率数据）。
_ODDS_TXT_COMPETITIONS = {"mls", "efl_cup"}


def _has_af_historical_data(db: Session, comp: "models.Competition") -> bool:
    """2022-2024 这三个赛季已经踢完、永远不会再变（api_football.py 顶部
    注释自己也是这么说的："不变量，第一次抓完就一直在库里，后续失败无损失"）。
    但代码原来没把这句话当真——每一轮 run_full_update（含定时任务和手动点
    「立即更新」，一天好几次）都会重新对 API-Football 发 6 个请求（mls+
    efl_cup 各 3 个赛季），一次都不省，纯粹为了重新确认一遍已经确认过的
    历史比分。免费档配额有限，这种白打的请求会跟当天真正需要的请求
    （当前赛季赛果、下一轮赛程）抢配额，实测就是这样把账号打到
    "Your account is suspended" 的——用户在 API-Football 后台看不出异常，
    因为那多半是配额触发的临时限制，不是真的账号问题。

    这里只要这个赛事在 2022-2024 窗口内已经有一场"已完赛"的比赛落库，
    就说明上一次抓取成功过，不需要再抓——upsert_matches 对已有比分的比赛
    是空操作，重抓不会带来任何新信息。"""
    from .api_football import FREE_TIER_SEASONS
    window_start = date_cls(FREE_TIER_SEASONS[0], 1, 1)
    window_end = date_cls(FREE_TIER_SEASONS[-1] + 1, 1, 1)
    return db.query(models.Match).filter(
        models.Match.competition_id == comp.id,
        models.Match.status == "played",
        models.Match.date >= window_start,
        models.Match.date < window_end,
    ).first() is not None


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
    # Ligue 1 —— openfootball 从 2023 年起换了写法，同一家俱乐部在
    # 2015-2023 和 2023-2026 两段用了不同名字。不合并会被当成两支球队、
    # 各拿一半比赛，两个参数都是错的。判据不是"名字像"，是**年份互补**
    # （旧名到 2023 为止、新名从 2023 开始，中间无重叠），实测场次：
    #   Olympique Marseille 294 → Olympique de Marseille 102（合并后 396）
    #   Stade Rennais       294 → Stade Rennais FC 1901  102（合并后 396）
    #   RC Strasbourg       217 → RC Strasbourg Alsace   102（合并后 319）
    #   Racing Club de Lens 102 → RC Lens                114（合并后 216）
    "Olympique Marseille": "Olympique de Marseille",
    "Stade Rennais": "Stade Rennais FC 1901",
    "RC Strasbourg": "RC Strasbourg Alsace",
    "Racing Club de Lens": "RC Lens",
    # 两个**不能**合并的，都单独查证过，别看着像就并进去：
    #   "Paris" 不是巴黎圣日耳曼，是巴黎FC（2025-26 升上法甲）。两者在
    #   2025、2026 两季同时出现，且直接交手过 2 场（2026-01-04、2026-05-17）,
    #   铁证是两家俱乐部。但裸写 "Paris" 太容易被误读成 PSG，所以跟 MLS
    #   的 "Los Angeles" 一样补全成正式名。
    "Paris": "Paris FC",
    #   AC Ajaccio(2022-23) 与 Gazélec FC Ajaccio(2015-16) 年份也互补，
    #   但这是科西嘉阿雅克肖同城的两家不同俱乐部，不是改名，不合并。
    # MLS —— 上面那条"去掉尾部 FC/AFC"的规则是照欧洲联赛的习惯写的，那边
    # "Arsenal FC" 的 FC 只是通用后缀，去掉不影响识别。但"Los Angeles FC"
    # （官方队名就叫 LAFC）不一样，FC 是队名本身的一部分，去掉之后变成
    # 光秃秃的"Los Angeles"——美职联同时有 Los Angeles Galaxy 和这支队，
    # "Los Angeles" 单独存在会让人分不清是哪一支，用真实抓下来的
    # API-Football 数据（2022-2024 三个赛季）核实过确实会被削成这样，
    # 必须专门排除。
    "Los Angeles": "Los Angeles FC",
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
#
# 欧冠原来被当成例外留在只走 .json 那条路——注释曾经写着"欧冠没有对应的
# .txt 源"，но 2026-08-04 实测发现这是错的：openfootball/champions-league
# 仓库有 {season}/cl.txt，跟其他联赛一样按赛季发布，只是欧冠的赛季文件是
# 完赛后才补全的（新赛季抽签在 8 月底才进行，抽签前那一季自然没有文件）。
# 加上这条源之后，「当前赛季 .json/.txt 都还没有」时会自动退到上一季——
# 而上一季（2025-26）恰好是刚踢完全部 189 场的完整数据，比 .json 镜像
# 卡住不动的 2024-25 新了一整年，对 MLE 训练和回测都更有意义。
# 2026-27 赛季本身要等抽签后（约 8 月 27 日）才会有任何免费源发布赛程，
# 在那之前"预测"页看不到欧冠的未来比赛是正确行为，不是 bug。
_TXT_SOURCES = {
    "en.1": "https://raw.githubusercontent.com/openfootball/england/master/{season}/1-premierleague.txt",
    "es.1": "https://raw.githubusercontent.com/openfootball/espana/master/{season}/1-liga.txt",
    "it.1": "https://raw.githubusercontent.com/openfootball/italy/master/{season}/1-seriea.txt",
    "de.1": "https://raw.githubusercontent.com/openfootball/deutschland/master/{season}/1-bundesliga.txt",
    "uefa.cl": "https://raw.githubusercontent.com/openfootball/champions-league/master/{season}/cl.txt",
    # 英冠。实测 2026-08：england/2026-27/2-championship.txt 已发布（200），
    # 而 football.json/2026-27/en.2.json 还是 404——跟英超一模一样的镜像
    # 滞后，所以这条 .txt 源是英冠能看到未来赛程的唯一途径。
    "en.2": "https://raw.githubusercontent.com/openfootball/england/master/{season}/2-championship.txt",
    # 法甲没有可用的 .txt 源：openfootball/france 仓库实测没有任何赛季的
    # ligue1 赛程文件（各种文件名/赛季组合都试过，全 404），europe 仓库同样
    # 没有。所以法甲只能靠 .json 镜像，而镜像目前最新只到 2025-26（已完赛）。
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
# 已完赛（各国联赛源，如 england/espana/italy/deutschland）：
# "19:00   Liverpool  4-2 (1-0)  Bournemouth"——没有 "v"，半场比分可有可无
_TXT_RESULT = re.compile(
    r"^\s+(?:(\d{1,2}):(\d{2})\s+)?(\S.*?)\s+(\d{1,2})-(\d{1,2})(?:\s+\([\d\-]+\))?\s+(\S.*?)\s*$")
# 已完赛（champions-league 源）：队名顺序跟联赛源不一样，中间带字面的 " v "——
# "Athletic Club (ESP)  v  Arsenal FC (ENG)  0-2 (0-0)"。用上面那条 _TXT_RESULT
# 去套这种行会出错：它的"半场比分"可选组要求组后面还有空白+队名，这里没有
# （比分是最后一段），于是那个可选组匹配失败又回溯，最终把 " v 对方队名" 也
# 吞进第一支队伍、把 "(0-0)" 错当成第二支队伍——两支队名全部错位。
# 所以欧冠这种"v 在比分前面"的格式必须单独一条规则，在 _TXT_RESULT 之前先试。
#
# 淘汰赛加时/点球会在比分后面追加标注，两种真实见过的写法：
#   "3-2 a.e.t. (3-0, 1-0)"            —— 加时赛，3-2 已经是最终真实进球比分
#   "4-3 pen. 1-1 a.e.t. (1-1, 0-1)"   —— 点球淘汰，4-3 是点球大战比分（不是
#                                          进球），真正的比赛进球比分是点球
#                                          大战比分后面那对 "1-1"
# 取分逻辑：有 "pen." 就用它后面那对数字（真实进球数），没有就用最前面那对
# （不管有没有 "a.e.t."，因为"加时后"的比分本身就是真实进球数）。末尾的
# 分段比分（半场/加时分段）只做展示用，不影响这里，直接忽略。
_TXT_RESULT_V = re.compile(
    r"^\s+(?:(\d{1,2}):(\d{2})\s+)?(\S.*?)\s+v\s+(\S.*?)\s+"
    r"(\d{1,2})-(\d{1,2})"
    r"(?:\s+pen\.\s+(\d{1,2})-(\d{1,2}))?"
    r"(?:\s+a\.e\.t\.)?"
    r"(?:\s+\([^)]*\))?\s*$")
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
    cur_time = None          # 没写时间的比赛沿用同一天上一场的时间（源文件就这么排的）
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
            cur_time = None          # 换一天就重置，别把前一天的时间带过来
            continue

        if cur_date is None:
            continue

        m = _TXT_RESULT_V.match(line)
        if m:
            h, mi, t1, t2, sh, sa, psh, psa = m.groups()
            if h:
                cur_time = f"{int(h):02d}:{mi}"
            # 有点球大战比分的话，前面那对是点球（不是进球），真正的
            # 进球比分是 "pen." 后面那对；见 _TXT_RESULT_V 上方注释。
            fh, fa = (psh, psa) if psh else (sh, sa)
            out.append({"date": cur_date.isoformat(), "round": cur_round, "time": cur_time,
                        "team1": t1.strip(), "team2": t2.strip(),
                        "score": {"ft": [int(fh), int(fa)]}})
            continue

        m = _TXT_RESULT.match(line)
        if m:
            h, mi, t1, sh, sa, t2 = m.groups()
            if h:
                cur_time = f"{int(h):02d}:{mi}"
            out.append({"date": cur_date.isoformat(), "round": cur_round, "time": cur_time,
                        "team1": t1.strip(), "team2": t2.strip(),
                        "score": {"ft": [int(sh), int(sa)]}})
            continue

        m = _TXT_FIXTURE.match(line)
        if m:
            h, mi, t1, t2 = m.groups()
            if h:
                cur_time = f"{int(h):02d}:{mi}"
            out.append({"date": cur_date.isoformat(), "round": cur_round, "time": cur_time,
                        "team1": t1.strip(), "team2": t2.strip(), "score": None})

    return out


def get_active_competitions(db: Session):
    return db.query(models.Competition).filter(
        models.Competition.is_active == True,
        models.Competition.data_source.isnot(None),
    ).all()


def _resolve_data_source(comp: models.Competition, start_back: int = 0):
    """挑出这个赛事该用哪个文件，返回 (url, 格式)，格式是 "json" 或 "txt"。

    start_back 表示「从当前赛季往前数第几季开始找」。默认 0 即当前赛季。
    fetch_results 在当前赛季一场比分都没有（新赛季只发了赛程）时会用
    start_back=1 再取一次，理由见那里的注释。

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

    start_year = int(guess_current_season().split("-")[0]) - start_back
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


def _fetch_matches(comp: models.Competition, start_back: int = 0) -> list:
    """取回这个赛事的全部比赛记录，统一成 football.json 的形状。

    fetch_results 和 fetch_upcoming 原来各自跑一遍探测阶梯再各自 GET 一次
    ——同一个文件下载两遍，探测也做两遍。合并到这里，两边都只是在这份
    结果上做过滤。
    """
    url, fmt = _resolve_data_source(comp, start_back=start_back)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    if fmt == "txt":
        # requests 对 text/plain 会按 ISO-8859-1 猜编码，而这些文件是 UTF-8
        # （München、Atlético 都会被毁掉）。显式指定，别让它猜。
        r.encoding = "utf-8"
        return parse_openfootball_txt(r.text)
    return r.json()["matches"]


def fetch_results(comp: models.Competition) -> list:
    played = [m for m in _fetch_matches(comp) if _extract_final_score(m.get("score")) is not None]
    if played or "{season}" not in comp.data_source:
        return played

    # 当前赛季一场比分都没有——新赛季刚发布赛程、还没开踢时就是这样。
    # 这时要再往回取一季的结果，否则一个新建的库里联赛将没有任何历史比分：
    # 回测页只剩世界杯和欧冠，贝叶斯状态也无从更新。
    #
    # 只在「当前赛季零结果」时才多跑这一次，赛季一开踢就不会再触发。
    return [m for m in _fetch_matches(comp, start_back=1)
            if _extract_final_score(m.get("score")) is not None]


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
            if m.get("time") and existing.time_utc != m["time"]:
                existing.time_utc = m["time"]
        else:
            db.add(models.Match(
                competition_id=comp.id, date=date_cls.fromisoformat(d),
                time_utc=m.get("time"),
                team1=t1, team2=t2, score1=s1, score2=s2,
                round=m.get("round", ""), grp=m.get("group", "KO"),
                ground=m.get("ground", ""), status="played",
            ))
            updated += 1

    for m in upcoming:
        t1, t2, d = normalize_team_name(m["team1"]), normalize_team_name(m["team2"]), m["date"]
        # 这里原来还在每场比赛查一次库——上面 played 那半边改成批量了，
        # 这半边漏掉了。赛程占绝大多数（1446/1739），漏的正是重的那一半。
        existing = existing_by_key.get((date_cls.fromisoformat(d), t1, t2))
        if existing:
            # 上游把「整轮压在一个暂定时间」换成真实开球时间时要跟着改，
            # 否则界面上永远显示第一次抓到的那个暂定值
            if m.get("time") and existing.time_utc != m["time"]:
                existing.time_utc = m["time"]
                updated += 1
        else:
            new_match = models.Match(
                competition_id=comp.id, date=date_cls.fromisoformat(d),
                time_utc=m.get("time"),
                team1=t1, team2=t2, round=m.get("round", ""), grp="KO",
                ground=m.get("ground", ""), status="upcoming",
            )
            db.add(new_match)
            # 同一轮里同队同日的重复对阵不会有，但把新建的也放进字典，
            # 保证「先出现在 played 再出现在 upcoming」这类顺序不会重复插入
            existing_by_key[(date_cls.fromisoformat(d), t1, t2)] = new_match
            updated += 1

    db.commit()
    return updated


def _canonical_team(name: str) -> str:
    """美职联/联赛杯专用：先过 The Odds API 的全称→简称别名表，再过通用归一化。

    两个数据源对同一支球队的写法不一样（The Odds API 用官方全称
    "Wolverhampton Wanderers"，API-Football 训练数据用简称 "Wolves"），
    这个函数给出唯一的规范形式。幂等——对已经是简称的名字再跑一次结果不变。
    """
    return normalize_team_name(odds_api.TEAM_ALIASES.get(name, name))


def dedupe_alias_renamed_matches(db: Session) -> int:
    """把用旧队名存下来的比赛归一到规范队名，并合并因此产生的重复记录。

    为什么需要这个：`upsert_matches` 认的是 (日期, 主队, 客队) 三元组。
    别名表是后来才加的——加之前那次更新把 The Odds API 的原名
    ("Wolverhampton Wanderers") 存进了库，加之后同一场比赛变成了 "Wolves"，
    三元组对不上，于是**又插了一条**。生产环境上的实际表现：同一场
    "狼队 vs 波特维尔" 在预测页出现两次，而且两条的概率还不一样——
    一条能查到 "Wolves" 的真实攻防参数（98.2%），另一条
    "Wolverhampton Wanderers" 在联赛杯参数表里查无此队、退回兜底值
    （80.6%）。用真实数据本地复现过，两个数字跟线上截图对得上。

    只处理 _ODDS_TXT_COMPETITIONS 里那两个赛事。**绝对不能对全部赛事跑**：
    "Wolverhampton Wanderers"、"Leicester City"、"West Ham United" 等恰好是
    英超那张 club 参数表里本来就正确的队名，对英超套用这张别名表会把
    正确的队名改坏——这跟当初别名表必须单独放在 odds_api.py、不能并进
    全局 _CLUB_NAME_ALIASES 是同一个理由。

    合并时保留哪一条：优先留已完赛的（有比分的那条信息更多），其次留
    id 小的（更早插入，更可能已经挂了用户的注单）。被删那条的子记录
    （赔率/虚拟盘注单/实盘注单/串关腿）全部改指到保留的那条——直接删会
    让用户的下注记录变成孤儿，那比重复显示严重得多。预测表是 match_id
    唯一约束，被删那条的预测只能删掉，下游 update_predictions() 会按
    保留下来的比赛重新生成。

    返回删掉的重复记录数。没有重复时是个纯读操作，不写库。
    """
    comps = db.query(models.Competition).filter(
        models.Competition.code.in_(_ODDS_TXT_COMPETITIONS)
    ).all()
    removed = 0

    for comp in comps:
        rows = db.query(models.Match).filter_by(competition_id=comp.id).all()
        groups = {}
        for m in rows:
            key = (m.date, _canonical_team(m.team1), _canonical_team(m.team2))
            groups.setdefault(key, []).append(m)

        for (d, ct1, ct2), members in groups.items():
            # 已完赛的排前面，同类按 id 升序 —— 第一个就是要保留的那条
            members.sort(key=lambda m: (0 if m.score1 is not None else 1, m.id))
            keeper, losers = members[0], members[1:]

            for lo in losers:
                # 保留的那条如果没有比分、而被删的那条有，把比分搬过去，
                # 否则会把已经踢完的结果丢掉
                if keeper.score1 is None and lo.score1 is not None:
                    keeper.score1, keeper.score2 = lo.score1, lo.score2
                    keeper.status = lo.status
                if not keeper.time_utc and lo.time_utc:
                    keeper.time_utc = lo.time_utc
                if not keeper.round and lo.round:
                    keeper.round = lo.round

                for model_cls in (models.Odds, models.Bet, models.RealBet, models.ParlayLeg):
                    db.query(model_cls).filter_by(match_id=lo.id).update(
                        {"match_id": keeper.id}, synchronize_session=False)
                db.query(models.Prediction).filter_by(match_id=lo.id).delete(
                    synchronize_session=False)
                db.delete(lo)
                removed += 1

            # 顺手把保留下来的这条也归一到规范队名，否则下一轮更新又会
            # 因为三元组对不上而重新插一条——只修合并、不改名字的话，
            # 这个 bug 每 12 小时就会复发一次
            if keeper.team1 != ct1 or keeper.team2 != ct2:
                keeper.team1, keeper.team2 = ct1, ct2

    db.commit()

    # 合并之后要查一种脏数据：同一注串关的两条腿指向了同一场比赛。
    #
    # 怎么产生的：/api/parlay-bets 是按 match_id 校验"同一场不能出现两次"的，
    # 而重复的那两条记录 match_id 不同（一条旧队名、一条规范队名），校验
    # 放行了。用户如果把界面上那两张长得一样的卡都选进同一注，就会建出
    # 一个「把同一件事当成两个独立事件连乘」的串关——概率被平方，记录本身
    # 就是错的。去重把两条腿指到同一场之后，这个错误才浮出水面。
    #
    # 刻意不自动删腿：那是在改用户已经下过的投注记录，比留着一条标记出来的
    # 脏数据更糟。只报出来，让用户自己决定要不要作废重记。
    suspects = []
    leg_rows = db.query(models.ParlayLeg.parlay_bet_id, models.ParlayLeg.match_id).all()
    by_parlay = {}
    for pid, mid in leg_rows:
        by_parlay.setdefault(pid, []).append(mid)
    for pid, mids in by_parlay.items():
        if len(set(mids)) != len(mids):
            suspects.append(pid)
    if suspects:
        logging.getLogger("valuebet.updater").warning(
            "[dedupe] 以下串关注单的两条腿指向同一场比赛，记录的联合概率是错的"
            "（同一件事被当成两个独立事件连乘），建议作废重记: parlay_bet_id=%s",
            suspects)

    return removed


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
            # 先清掉历史遗留的重复（旧队名 vs 规范队名各存了一条），再抓新数据。
            # 顺序不能反：先抓的话，新抓回来的规范队名又会跟旧队名那条并存，
            # 这一轮结束时库里仍然是重复的。
            n_deduped = dedupe_alias_renamed_matches(db)
            if n_deduped:
                logging.getLogger("valuebet.updater").info(
                    "[dedupe] 合并了 %d 条队名不一致导致的重复比赛", n_deduped)
            for comp in get_active_competitions(db):
                # 每个赛事单独 try —— 一个赛事的数据源挂了（比如某季文件还没
                # 发布），不该连累其他赛事。实测踩过：欧冠 404 导致整个事务回滚，
                # 另外5个赛事的预测一条都没生成。
                try:
                    if comp.code in _ODDS_TXT_COMPETITIONS:
                        # 三路数据各自独立成败，不互相拖累：
                        #   历史赛果(API-Football) —— 2022-2024，不变量，
                        #     第一次抓完就一直在库里，后续失败无损失
                        #   当前赛季赛果(The Odds API /scores) —— 决定注单
                        #     能不能结算，最不能丢的一路
                        #   未来赛程(The Odds API /events) —— 决定有没有得预测
                        #
                        # 早先的写法是历史那路直接抛出去，结果 API-Football
                        # 一旦挂了（额度用尽、服务抖动），当前赛季的赛果也跟着
                        # 抓不成，用户的注单就一直挂着不结算——而那批历史数据
                        # 明明早就在库里了，重抓与否根本不影响什么。
                        #
                        # 「不影响什么」不该只停在嘴上——已经在库里就真的不再
                        # 重抓，见 _has_af_historical_data。省下来的不只是几个
                        # 请求：这三个赛季的数据不变，之前是每轮都重新问一遍
                        # 已经问过的问题，白白跟当天真正要紧的请求（当前赛季
                        # 赛果、下一轮赛程）抢免费档配额。
                        steps = [("当前赛季赛果", lambda: odds_api.fetch_recent_scores(comp.code))]
                        if not _has_af_historical_data(db, comp):
                            steps.insert(0, ("历史赛果", lambda: api_football.fetch_training_matches(comp.code)))
                        played, sub_fail = [], []
                        for label, fn in steps:
                            try:
                                played += fn()
                            except Exception as se:
                                sub_fail.append("%s: %s" % (label, str(se)[:120]))
                                logging.getLogger("valuebet.updater").warning(
                                    "[%s] %s 抓取失败: %s", comp.code, label, str(se)[:200])
                        # 赛程这一路仍然直接抛：它失败意味着这个赛事这一轮
                        # 完全没有可预测的比赛，属于真的该标红的情况
                        upcoming = odds_api.fetch_upcoming_events(comp.code)
                        if sub_fail:
                            # 不静默——半成功也要在界面的状态条上看得见
                            failed_comps.append("%s（部分）: %s" % (comp.code, "; ".join(sub_fail)))
                    else:
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
