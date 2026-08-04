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
            "team1": ev["home_team"],
            "team2": ev["away_team"],
            "round": "",
        })
    return out
