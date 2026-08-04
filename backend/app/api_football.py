"""
API-Football (api-sports.io) 客户端 —— 目前只给美职联、英格兰联赛杯这两个
openfootball 完全没有覆盖的赛事提供历史赛果，用来训练各自独立的参数表。

为什么只用它抓「历史」，「即将开赛」的赛程走 odds_api.py：
    免费档明确限制只能查 2022-2024 三个赛季（实测：查 2025/2026 直接返回
    errors.plan = "Free plans do not have access to this season, try from
    2022 to 2024."）。这三季踢完的比赛足够训练参数，但拿不到当前/未来的
    赛程——而这恰恰是"能不能拿去下注"最核心的那部分。The Odds API 这边
    反而没有这个限制，/events 接口能查到真实的未来赛程（已用真实请求验证：
    2026-08 能查到美职联到 8月20日、联赛杯到 8月10日的对阵）。

    两边职责因此分开：API-Football 管"过去"（训练），The Odds API 管
    "未来"（预测对象）。见 odds_api.py。
"""
import os
import requests

API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
_BASE = "https://v3.football.api-sports.io"

# 免费档实测只放行这三个赛季（2026-08-04 用真实 key 验证过，见对话记录）。
# 以后升级付费档解锁当前赛季，把这个列表加长即可，不用改调用逻辑。
FREE_TIER_SEASONS = [2022, 2023, 2024]

# 已用真实 /leagues?search= 请求核实过的赛事 id，不是查文档猜的：
#   美职联 —— search=MLS 命中的是"MLS Next Pro"（预备队联赛，不是这个）和
#   "MLS All-Star"（表演赛），真正的美职联要搜全称 "Major League Soccer"
#   才能找到，id=253。
#   联赛杯 —— search=League%20Cup 全球同名赛事有 16 个（新加坡、南非、
#   北爱尔兰……），英格兰这个 country=England 的 id=48。
LEAGUE_IDS = {
    "mls": 253,
    "efl_cup": 48,
}

# 判定"打完了"用 API-Football 自己的状态枚举，比猜比分字段在不在更可靠
# （FT=常规时间结束，AET=加时后结束，PEN=点球大战决出胜负；NS/TBD/CANC/PST
# 等都不算打完）。真实样本验证过 AET/PEN 两种情况：
#   goals.home/away 字段在这两种情况下都已经是"真实进球数"（含加时进球，
#   不含点球大战比分）——洛杉矶FC 1-2 西雅图那场 AET，goals=(1,2)、
#   fulltime=(1,1)、extratime=(0,1)，goals 就是最终真实比分，直接拿来用
#   即可，不需要像 openfootball 那样另外从 "pen." 后面找真实比分。
_PLAYED_STATUSES = {"FT", "AET", "PEN"}


def fetch_af_season(league_id: int, season: int) -> list:
    """抓一个联赛一个赛季的全部比赛，返回原始 fixtures 数组（未过滤）。"""
    if not API_FOOTBALL_KEY:
        # 本地没配这个 key 是常态（DEPLOY.md 的原则：不配环境变量也要能跑），
        # 不该让整个更新任务崩掉——抛出去，run_full_update 里每个赛事单独
        # try/except，会记成这一个赛事的 partial 失败，其余赛事不受影响。
        raise RuntimeError("API_FOOTBALL_KEY 未配置，跳过美职联/联赛杯的历史数据抓取")
    r = requests.get(
        f"{_BASE}/fixtures",
        params={"league": league_id, "season": season},
        headers={"x-apisports-key": API_FOOTBALL_KEY},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("errors"):
        raise RuntimeError(f"API-Football 返回错误: {data['errors']}")
    return data.get("response", [])


def played_matches_from_fixtures(fixtures: list) -> list:
    """把 API-Football 的 fixture 数组转成跟 openfootball 同形的记录
    （date / team1 / team2 / score / round / time），只留真正打完的比赛。

    score 包成 {"ft": [h, a]} 是故意的——直接复用 updater.py 里已经在用的
    _extract_final_score()，不用再写一套解析。
    """
    out = []
    for fx in fixtures:
        status = (fx.get("fixture", {}).get("status", {}) or {}).get("short")
        if status not in _PLAYED_STATUSES:
            continue
        goals = fx.get("goals", {})
        gh, ga = goals.get("home"), goals.get("away")
        if gh is None or ga is None:
            continue
        fixture_date = fx["fixture"]["date"]  # "2024-08-13T18:30:00+00:00"
        out.append({
            "date": fixture_date[:10],
            "time": fixture_date[11:16],
            "team1": fx["teams"]["home"]["name"],
            "team2": fx["teams"]["away"]["name"],
            "round": fx.get("league", {}).get("round", ""),
            "score": {"ft": [gh, ga]},
        })
    return out


def fetch_training_matches(league_code: str) -> list:
    """抓 FREE_TIER_SEASONS 里全部赛季的已完赛比赛，训练用。"""
    league_id = LEAGUE_IDS[league_code]
    out = []
    for season in FREE_TIER_SEASONS:
        fixtures = fetch_af_season(league_id, season)
        out.extend(played_matches_from_fixtures(fixtures))
    return out
