"""
The Odds API 客户端 —— 赛程、比分、以及（2026-08 起）1X2 盘口报价。

原来的模块说明写着「不请求任何带赔率/盘口的接口（比如 /odds）……这里没有、
也不会往输入框自动填任何数字」。**这条自我约束现在解除了**，原文保留在这里
当记录，因为改的是当初明确承诺过的事，不该悄悄改掉：

    「明确不做的事：不请求任何带赔率/盘口的接口（比如 /odds），只用
      /events——这个接口只返回赛程骨架，不含任何一家博彩公司的报价。
      用户手动输入赔率、系统才计算 EV 的既定流程不受影响。」

解除的理由：那条约束保护的是「EV 必须用市场原始赔率、不能用去抽水的市场
概率反推」这件事，而它保护的方式是干脆不碰赔率数据。用户每天手工敲几十
场的赔率，效率低到影响正常使用，这个代价当初低估了。

真正的约束一条都没松，写清楚免得以后误读：
  · EV 公式还是 `模型概率 × 市场原始赔率 - 1`。自动抓回来的赔率放在
    **市场报价**这一路，只做「预填输入框」和「比价」，绝不会被当成模型
    自己的概率。CLAUDE.md 里唯一还禁止的那个循环用法（把去抽水的市场
    概率当预测再乘市场赔率）跟这里无关，也没有被打开。
  · 不做自动下注。抓的是公开报价，不碰任何博彩账号、不提交任何投注。

同时给最优价和平均价，是因为项目自己走查出来的结论：热门-冷门偏差那条唯一
为正的路子，盈亏完全取决于价格执行（f=0 时 ROI -3.54%，f=0.8 才盈亏平衡）。
只存一个价的话这个差额无从衡量，见 ev_evidence.py 和 handoff/09。
"""
import os
import logging
import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
_BASE = "https://api.the-odds-api.com/v4"

# 已用真实 key 核实过的 sport key（GET /v4/sports/ 返回的列表里逐个比对，
# 不是猜的）：美职联 soccer_usa_mls，英格兰联赛杯 soccer_england_efl_cup
# （标题写的就是 "League Cup"，跟 API-Football 的 id=48 是同一个赛事）。
SPORT_KEYS = {
    "mls": "soccer_usa_mls",
    "efl_cup": "soccer_england_efl_cup",
    # 英甲/英乙/西乙：这三个 key 是从 /v4/sports/ 的真实返回里挑出来的，
    # 不是按命名规律推的。同一份返回里**没有**英格兰全国联赛——第五级
    # 半职业联赛博彩公司不开盘，The Odds API 就不收录，所以全国联赛拿不到
    # 未来赛程，只能等 openfootball 发布，这是数据源的事实不是配置问题。
    "leagueone": "soccer_england_league1",
    "leaguetwo": "soccer_england_league2",
    "segunda": "soccer_spain_segunda_division",
    # 下面这批是为了**抓赔率**才加的（赛程照旧走 openfootball，不受影响：
    # 走不走 The Odds API 拿赛程由 _ODDS_FIXTURE_COMPETITIONS 单独控制，
    # 这几个不在那个集合里）。key 同样是从你真机 /v4/sports/ 的 45 个
    # soccer key 里逐个比对出来的，不是按命名规律推的。
    "epl": "soccer_epl",
    "laliga": "soccer_spain_la_liga",
    "seriea": "soccer_italy_serie_a",
    "bundesliga": "soccer_germany_bundesliga",
    "ligue1": "soccer_france_ligue_one",
    "championship": "soccer_efl_champ",
    # 缺两个，都是数据源的事实不是漏配：
    #   欧冠 —— 那份返回里只有 soccer_uefa_champs_league_qualification
    #     （资格赛），正赛的 key 不在列表里。八月初正赛还没开盘，博彩公司
    #     没挂盘 The Odds API 就不收录。等赛季开始后重新查一次 /sports 再补。
    #   英格兰全国联赛 —— 第五级半职业联赛博彩公司不开盘，同 fetch 赛程
    #     那边的结论，不会有。
}

# 训练数据（历史赛果）来自 API-Football，这里（未来赛程）来自 The Odds
# API，两边对同一支队伍用的名字风格完全不同：API-Football 习惯简称
# （"Wolves"、"QPR"、"Sheffield Utd"），The Odds API 用官方全称
# （"Wolverhampton Wanderers"、"Queens Park Rangers"、"Sheffield United"）。
# 不转换的话，同一支球队的"已完赛"记录和"即将开赛"记录会存成两个不同
# 的队名字符串——不仅拿不到训练好的攻防参数（静默退回联赛平均水平，
# 不报错），upsert_matches 用 (日期,主队,客队) 去重时还会把同一场比赛
# 存成两条不同的记录（一条永远停在"未开赛"，一条是后来插入的"已完赛"）。
#
# 特意没有放进 updater.py 那张全局 _CLUB_NAME_ALIASES：第一版实现试过
# 直接塞进去，测出这里面"Wolverhampton Wanderers"、"Cardiff City"、
# "Leicester City"、"Norwich City"、"West Ham United"、"Sheffield United"
# 等名字，恰好也是英超/英冠联合训练那张 club 表（164队）本来就在用的
# 正确队名——放进全局表会让这 9 支英超球队的预测全部悄悄查到错误参数，
# 比不修复更严重。所以单独放这儿，只在处理这两个赛事的赛程时应用。
# 不需要放进 updater.py 还有个原因：那边已经 `from . import odds_api`，
# 反过来在这里 import updater 会循环引用。
#
# 表本身是拿 2026-08-04 真实抓下来的 API-Football 三季历史 + The Odds
# API 当天真实赛程逐个比对出来的，不是猜的：凡是 The Odds API 给出的
# 队名在训练数据的完整队伍名单（不只是参数达标的那些）里找不到对应项，
# 才收进这张表。"San Diego FC"（2025年才加入美职联的扩军队）、"Barnet"
# （联赛杯今年才首次入围的对阵）这类训练数据里压根不存在的新队不在表
# 里——那是真的没有历史数据，退回联赛平均水平是唯一能做的，不是命名
# 对不上，加别名也解决不了。
TEAM_ALIASES = {
    "D.C. United": "DC United",
    "Columbus Crew SC": "Columbus Crew",
    "LA Galaxy": "Los Angeles Galaxy",
    "Inter Miami CF": "Inter Miami",
    "St. Louis City SC": "St. Louis City",
    "Wolverhampton Wanderers": "Wolves",
    "Wycombe Wanderers": "Wycombe",
    "Queens Park Rangers": "QPR",
    "Crewe Alexandra": "Crewe",
    "Accrington Stanley": "Accrington ST",
    "Wigan Athletic": "Wigan",
    "Swansea City": "Swansea",
    "Birmingham City": "Birmingham",
    "Blackburn Rovers": "Blackburn",
    "Grimsby Town": "Grimsby",
    "Bolton Wanderers": "Bolton",
    "Bradford City": "Bradford",
    "Peterborough United": "Peterborough",
    "Cardiff City": "Cardiff",
    "Derby County": "Derby",
    "Lincoln City": "Lincoln",
    "Doncaster Rovers": "Doncaster",
    "Preston North End": "Preston",
    "Huddersfield Town": "Huddersfield",
    "Leicester City": "Leicester",
    "Northampton Town": "Northampton",
    "Norwich City": "Norwich",
    "Wimbledon": "AFC Wimbledon",
    "West Ham United": "West Ham",
    "Shrewsbury Town": "Shrewsbury",
    "Cheltenham Town": "Cheltenham",
    "Charlton Athletic": "Charlton",
    "Colchester United": "Colchester",
    "Rotherham United": "Rotherham",
    "West Bromwich Albion": "West Brom",
    "Sheffield United": "Sheffield Utd",
    "Plymouth Argyle": "Plymouth",
}


def fetch_upcoming_events(league_code: str) -> list:
    """返回跟 openfootball 的"未打的比赛"同形的记录（date / team1 / team2 /
    round / time），不含 score 字段——upsert_matches 对 upcoming 列表本来
    就不读 score，语义上这批本就还没开球。
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY 未配置，跳过美职联/联赛杯的赛程抓取")
    sport_key = SPORT_KEYS[league_code]
    r = requests.get(
        f"{_BASE}/sports/{sport_key}/events/",
        params={"apiKey": ODDS_API_KEY},
        timeout=20,
    )
    r.raise_for_status()
    events = r.json()

    out = []
    for ev in events:
        commence = ev["commence_time"]  # "2026-08-08T20:30:00Z"
        out.append({
            "date": commence[:10],
            "time": commence[11:16],
            "team1": TEAM_ALIASES.get(ev["home_team"], ev["home_team"]),
            "team2": TEAM_ALIASES.get(ev["away_team"], ev["away_team"]),
            "round": "",
        })
    return out


def _quota_from(resp) -> dict:
    """The Odds API 把配额放在响应头里。免费档一个月 500 次，抓赔率是最
    费配额的一路（赛程和比分都不算），所以每次都把剩余量带回去记进日志，
    别等配额用光了才发现。"""
    def _int(h):
        try:
            return int(resp.headers.get(h, ""))
        except (TypeError, ValueError):
            return None
    return {"remaining": _int("x-requests-remaining"), "used": _int("x-requests-used")}


def fetch_h2h_odds(league_code: str, regions: str = "eu") -> tuple:
    """抓一个联赛的 1X2 盘口，返回 (记录列表, 配额信息)。

    每条记录同时给**最优价**和**平均价**，两个都要，理由是项目自己走查出来
    的结论（handoff/09 + ev_evidence.py）：热门-冷门偏差这条唯一为正的路子，
    盈亏完全取决于价格执行——成交价 = 平均价 + f×(最优价-平均价)，f=0 时
    ROI -3.54%，f=0.8 才盈亏平衡。只存一个价的话，这个差额根本无从衡量。

    解析形状是照着用户真机跑出来、贴回来的真实返回写的，不是照文档猜的：
        [{"commence_time": "...", "home_team": "...", "away_team": "...",
          "bookmakers": [{"key": "...", "title": "...",
                          "markets": [{"key": "h2h",
                                       "outcomes": [{"name": 队名或"Draw",
                                                     "price": 2.15}]}]}]}]
    outcome 的 name 用的是队名全称（不是 home/away），平局固定是 "Draw"。
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY 未配置，跳过赔率抓取")
    if league_code not in SPORT_KEYS:
        raise RuntimeError(f"{league_code} 没有对应的 sport key，The Odds API 不收录这个赛事")

    r = requests.get(
        f"{_BASE}/sports/{SPORT_KEYS[league_code]}/odds/",
        params={"apiKey": ODDS_API_KEY, "regions": regions,
                "markets": "h2h", "oddsFormat": "decimal"},
        timeout=25,
    )
    r.raise_for_status()
    quota = _quota_from(r)

    out = []
    for ev in r.json():
        home_raw, away_raw = ev.get("home_team"), ev.get("away_team")
        if not home_raw or not away_raw:
            continue
        # 每家博彩公司一组报价，先按 主/平/客 归堆
        buckets = {"home": [], "draw": [], "away": []}
        books = 0
        for bk in ev.get("bookmakers", []):
            market = next((m for m in bk.get("markets", []) if m.get("key") == "h2h"), None)
            if not market:
                continue
            got = {}
            for oc in market.get("outcomes", []):
                name, price = oc.get("name"), oc.get("price")
                if not price or price <= 1.0:
                    # 赔率必须 >1，否则不是有效报价（1.0 意味着"押多少赔多少
                    # 都不赚"，真实盘口不会出现）。宁可丢掉也不要让脏数据
                    # 进到 EV 计算里去。
                    continue
                if name == "Draw":
                    got["draw"] = price
                elif name == home_raw:
                    got["home"] = price
                elif name == away_raw:
                    got["away"] = price
            # 三个结果不齐的那家跳过——只有齐了才能拿来比价
            if len(got) == 3:
                books += 1
                for k, v in got.items():
                    buckets[k].append(v)
        if books == 0:
            continue

        commence = ev.get("commence_time", "")
        out.append({
            "date": commence[:10],
            "time": commence[11:16],
            "team1": TEAM_ALIASES.get(home_raw, home_raw),
            "team2": TEAM_ALIASES.get(away_raw, away_raw),
            "n_books": books,
            "best_home": max(buckets["home"]), "best_draw": max(buckets["draw"]),
            "best_away": max(buckets["away"]),
            "avg_home": round(sum(buckets["home"]) / books, 3),
            "avg_draw": round(sum(buckets["draw"]) / books, 3),
            "avg_away": round(sum(buckets["away"]) / books, 3),
        })
    return out, quota


def _parse_score_entry(ev: dict) -> tuple:
    """从一条 /scores 记录里取出 (主队进球, 客队进球)，取不到就返回 None。

    刻意写得挑剔：这个函数是在**没有拿到真实返回验证过**的情况下写的
    （开发环境的网络策略连不上 the-odds-api.com，用浏览器也试过）。
    所以宁可什么都不返回，也不猜着解析——一个猜错的比分会被
    upsert_matches 当成真实赛果写进库，进而喂给贝叶斯更新、结算注单，
    是会污染数据的错误；而返回 None 只是"这场暂时没结算"，下一轮
    还能再来一次。失败要看得见、要可恢复。

    按 The Odds API v4 文档，scores 是 [{"name": 队名, "score": "2"}, ...]，
    队名跟同一条记录里的 home_team/away_team 对应。分数是字符串。
    """
    scores = ev.get("scores")
    if not isinstance(scores, list) or len(scores) < 2:
        return None
    home, away = ev.get("home_team"), ev.get("away_team")
    by_name = {}
    for s in scores:
        if not isinstance(s, dict):
            return None
        n, v = s.get("name"), s.get("score")
        if n is None or v is None:
            return None
        try:
            by_name[n] = int(str(v).strip())
        except (TypeError, ValueError):
            return None            # 分数不是整数 → 结构跟预期不符，放弃这条
    if home not in by_name or away not in by_name:
        return None                # 队名对不上，不敢猜哪个是主队
    return by_name[home], by_name[away]


def fetch_recent_scores(league_code: str, days_from: int = 3) -> list:
    """抓最近几天已完赛的比分，补上 API-Football 免费档拿不到的当前赛季赛果。

    为什么需要它：api_football.py 的免费档只放行 2022-2024 三个赛季，
    当前赛季的比分它一条都给不了。后果不是"少点数据"，是**在这两个
    赛事下的注永远不会自动结算、贝叶斯参数永远不更新、比赛永远停在
    "未开赛"**。/events 只给赛程骨架，补不了这个洞，/scores 才可以。

    返回记录跟 openfootball 的"已完赛"同形（date/time/team1/team2/score），
    直接喂给 upsert_matches 就能填进已存在的那条赛程记录。

    daysFrom 上限是 3（超出会被接口拒绝）。更新周期是 12 小时，3 天的
    回看窗口有足够重叠，某一次更新失败也不会漏掉比赛。

    额度提醒：/scores 带 daysFrom 时每次调用计 2 个额度，两个赛事、
    每天两轮 = 8/天 ≈ 240/月，免费档是 500/月。够用但不宽裕，
    以后如果再加赛事要重新算这笔账。
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY 未配置，跳过美职联/联赛杯的赛果抓取")
    sport_key = SPORT_KEYS[league_code]
    r = requests.get(
        f"{_BASE}/sports/{sport_key}/scores/",
        params={"apiKey": ODDS_API_KEY, "daysFrom": days_from},
        timeout=20,
    )
    r.raise_for_status()
    events = r.json()
    if not isinstance(events, list):
        raise RuntimeError(
            "/scores 返回的不是数组（实际是 %s）——接口结构跟预期不符，"
            "本轮不写入任何赛果" % type(events).__name__)

    out, skipped = [], 0
    for ev in events:
        if not isinstance(ev, dict) or not ev.get("completed"):
            continue                       # 只要真正踢完的
        commence = ev.get("commence_time")
        home, away = ev.get("home_team"), ev.get("away_team")
        if not (isinstance(commence, str) and len(commence) >= 16 and home and away):
            skipped += 1
            continue
        parsed = _parse_score_entry(ev)
        if parsed is None:
            skipped += 1
            continue
        h, a = parsed
        out.append({
            "date": commence[:10],
            "time": commence[11:16],
            "team1": TEAM_ALIASES.get(home, home),
            "team2": TEAM_ALIASES.get(away, away),
            "round": "",
            "score": {"ft": [h, a]},
        })

    if skipped:
        # 不静默吞掉。跳过的多说明接口结构可能变了，日志里要看得见
        logging.getLogger("valuebet.odds_api").warning(
            "[%s] /scores 有 %d 条完赛记录结构不符预期被跳过（成功解析 %d 条）",
            league_code, skipped, len(out))
    return out
