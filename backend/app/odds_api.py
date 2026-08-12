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
import re
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
    # 意乙。同样从你真机 /v4/sports/ 的 45 个 soccer key 里核对出来的——
    # 那份返回里意大利只有 soccer_italy_serie_a 和 soccer_italy_serie_b 两个。
    "serieb": "soccer_italy_serie_b",
    # 德乙/法乙。同样从那 45 个 key 里核对出来的：德国有 bundesliga /
    # bundesliga2 / dfb_pokal / liga3 四个，法国有 ligue_one / ligue_two 两个。
    "bundesliga2": "soccer_germany_bundesliga2",
    "ligue2": "soccer_france_ligue_two",
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


_KEY_RE = re.compile(r"(apiKey|api_key|apikey)=[^&\s\"\']*", re.I)


def _redact(text: str) -> str:
    """把任何形式的 API key 从文本里抹掉。

    不是洁癖，是真事故：requests 的 raise_for_status() 抛的异常消息里带着
    **完整请求 URL**，而这个 URL 的查询串里就是 apiKey。这条异常一路被
    run_full_update 收进 failed_comps → 存进 UpdateLog.detail → /api/status
    原样回给前端 → **直接显示在页面横幅上**。线上真的出现过：

        401 Client Error: Unauthorized for url:
        https://api.the-odds-api.com/v4/sports/soccer_usa_mls/scores/?apiKey=1f888a5c0d6

    key 被截断只是因为 detail 那里做了 [:120]，不是有意保护——换个短一点的
    赛事名就会把整把 key 打出来。任何看得到这个页面的人都能拿走它。

    所以在**抛出之前**就抹掉，而不是指望每个调用方记得处理。
    """
    return _KEY_RE.sub(r"\1=***", text or "")


def _get(url: str, params: dict, timeout: int = 20):
    """统一的请求入口：出错时抛不带 key 的异常。

    三个 fetch_* 原来各自 requests.get + raise_for_status，key 就是从那里
    漏出去的。收敛到一个函数，以后新增接口自动继承这个保护。
    """
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 401:
        # 401 的原因是确定的，直接给一句能照着做的话，不要甩原始 HTTP 错误
        raise RuntimeError("ODDS_API_KEY 无效或已失效（401）——"
                           "换过 key 的话记得同步改部署平台的环境变量")
    if r.status_code == 429:
        raise RuntimeError("The Odds API 配额用尽（429）——免费档每月 500 次，"
                           "下个账单周期恢复")
    try:
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(_redact(str(e))) from None
    return r


# 训练数据用 API-Football 简称的赛事。只有这两个才该套 TEAM_ALIASES——
# 其余赛事走 club 参数表，队名是 openfootball 全称，套了会改坏。
_SHORTNAME_LEAGUES = {"mls", "efl_cup"}


def fetch_upcoming_events(league_code: str) -> list:
    """返回跟 openfootball 的"未打的比赛"同形的记录（date / team1 / team2 /
    round / time），不含 score 字段——upsert_matches 对 upcoming 列表本来
    就不读 score，语义上这批本就还没开球。
    """
    if not ODDS_API_KEY:
        raise RuntimeError("ODDS_API_KEY 未配置，跳过美职联/联赛杯的赛程抓取")
    sport_key = SPORT_KEYS[league_code]
    r = _get(f"{_BASE}/sports/{sport_key}/events/", {"apiKey": ODDS_API_KEY})
    events = r.json()

    # TEAM_ALIASES **只**给美职联/联赛杯用，绝对不能对所有赛事套。
    #
    # 那张表把官方全称转成 API-Football 简称（"Bradford City"→"Bradford"、
    # "Leicester City"→"Leicester"），因为那两个赛事的训练数据就是用简称的。
    # 但英甲/英乙/西乙/意乙/德乙/法乙用的是 club 那张表，队名是 openfootball
    # 的**全称**——原名本来就对得上，套一遍反而改坏。
    #
    # 实测代价（线上状态条报出来的）：英甲 8 支、英乙 8 支球队的赛程被
    # 拒收，全是被这张表改坏的。加闸门之前这批比赛是**静默**进库的，
    # 队名对不上参数表就退回 (0,0)，界面照样显示看起来正常的概率。
    #
    # 当初核对 70 支球队时漏掉这一步：我是拿 Odds API 的原始队名直接跑
    # normalize_team_name，而真实链路是「先过 TEAM_ALIASES，再 normalize」。
    # 验证的路径跟生产的路径不一样，所以验证全绿、线上全错。
    use_aliases = league_code in _SHORTNAME_LEAGUES

    out = []
    for ev in events:
        commence = ev["commence_time"]  # "2026-08-08T20:30:00Z"
        h, a = ev["home_team"], ev["away_team"]
        if use_aliases:
            h, a = TEAM_ALIASES.get(h, h), TEAM_ALIASES.get(a, a)
        out.append({
            "date": commence[:10],
            "time": commence[11:16],
            "team1": h,
            "team2": a,
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


# 抓哪些地区的博彩公司。The Odds API 按 **地区 × 盘口** 计费：regions="eu"
# 一次请求 1 个配额，regions="uk,eu" 就是 2 个。免费档一个月 500 次，所以
# 默认只用 eu，要更多博彩公司自己在环境变量里加。
#
#   eu  —— Pinnacle、1xBet、Unibet、Betclic 等（Pinnacle 抽水最低，
#          是判断「这个价到底好不好」最有参考价值的一家）
#   uk  —— bet365、William Hill、Betfair、Ladbrokes 等
#   au / us —— 澳洲 / 美国的本地博彩公司
#
# 想要 bet365 就设 ODDS_API_REGIONS=uk,eu（配额翻倍，自己权衡）。
ODDS_API_REGIONS = os.environ.get("ODDS_API_REGIONS", "eu").strip() or "eu"


def fetch_h2h_odds(league_code: str, regions: str = None) -> tuple:
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

    r = _get(f"{_BASE}/sports/{SPORT_KEYS[league_code]}/odds/",
             {"apiKey": ODDS_API_KEY, "regions": regions or ODDS_API_REGIONS,
              "markets": "h2h", "oddsFormat": "decimal"}, timeout=25)
    quota = _quota_from(r)
    use_aliases = league_code in _SHORTNAME_LEAGUES

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
            # 同 fetch_upcoming_events：这张表只对 mls/efl_cup 有意义，
            # 对走 club 参数表的赛事套一遍会把正确的全称改成对不上的简称，
            # 结果是市场赔率**一场都匹配不上**（refresh_market_odds 会全部
            # 计成 unmatched 静默丢掉，配额白花）。
            "team1": (TEAM_ALIASES.get(home_raw, home_raw) if use_aliases else home_raw),
            "team2": (TEAM_ALIASES.get(away_raw, away_raw) if use_aliases else away_raw),
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
    use_aliases = league_code in _SHORTNAME_LEAGUES
    sport_key = SPORT_KEYS[league_code]
    r = _get(f"{_BASE}/sports/{sport_key}/scores/",
             {"apiKey": ODDS_API_KEY, "daysFrom": days_from})
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
            # 同上。这一路目前只有 mls/efl_cup 在用，但别名照样要按赛事判断，
            # 免得以后有别的赛事接进来时又踩一遍。
            "team1": (TEAM_ALIASES.get(home, home) if use_aliases else home),
            "team2": (TEAM_ALIASES.get(away, away) if use_aliases else away),
            "round": "",
            "score": {"ft": [h, a]},
        })

    if skipped:
        # 不静默吞掉。跳过的多说明接口结构可能变了，日志里要看得见
        logging.getLogger("valuebet.odds_api").warning(
            "[%s] /scores 有 %d 条完赛记录结构不符预期被跳过（成功解析 %d 条）",
            league_code, skipped, len(out))
    return out
