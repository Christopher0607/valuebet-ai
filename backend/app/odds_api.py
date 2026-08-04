"""
The Odds API 客户端 —— 只用来查"接下来有哪些比赛"，不拉赔率。

用途单一：给美职联、英格兰联赛杯这两个 API-Football 免费档查不到当前赛季
（见 api_football.py 顶部注释）的赛事提供未来赛程，让预测有对象可算。

明确不做的事：不请求任何带赔率/盘口的接口（比如 /odds），只用 /events——
这个接口只返回赛程骨架（比赛id、开球时间、主客队名），不含任何一家博彩公司
的报价。用户手动输入赔率、系统才计算 EV 的既定流程不受影响，这里没有、
也不会往输入框自动填任何数字。
"""
import os
import requests

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "").strip()
_BASE = "https://api.the-odds-api.com/v4"

# 已用真实 key 核实过的 sport key（GET /v4/sports/ 返回的列表里逐个比对，
# 不是猜的）：美职联 soccer_usa_mls，英格兰联赛杯 soccer_england_efl_cup
# （标题写的就是 "League Cup"，跟 API-Football 的 id=48 是同一个赛事）。
SPORT_KEYS = {
    "mls": "soccer_usa_mls",
    "efl_cup": "soccer_england_efl_cup",
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
