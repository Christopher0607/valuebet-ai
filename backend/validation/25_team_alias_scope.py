"""TEAM_ALIASES 的作用范围 —— 三个入口都要按赛事判断，一个都不能漏。

背景（真实踩过的 bug）：odds_api.TEAM_ALIASES 是给美职联/联赛杯建的，
把官方全称转成 API-Football 简称（"Bradford City"→"Bradford"），因为那两个
赛事的训练数据就是用简称的。

但 fetch_upcoming_events 对**所有**赛事无条件套了这张表。英甲/英乙/西乙/
意乙/德乙/法乙走的是 club 参数表，队名是 openfootball 的全称——原名本来就
对得上，套一遍反而改坏。线上表现：英甲 8 支、英乙 8 支球队的赛程被拒收。

第一次修只改了 fetch_upcoming_events，漏了 fetch_h2h_odds 和
fetch_recent_scores 两个同样的入口。fetch_h2h_odds 漏掉的后果更隐蔽：
市场赔率会**一场都匹配不上**，refresh_market_odds 全部计成 unmatched 静默
丢掉，配额白花而界面上只是"没有市场价"。

所以这个测试三个入口一起测，而且是**照真实链路**测（fetch → 别名 →
normalize → 查参数表），不是只测 normalize——当初验证全绿、线上全错，
正是因为验证走的路径跟生产走的路径不一样。
"""
import sys
import os
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app import odds_api                                             # noqa: E402
from app.updater import normalize_team_name                          # noqa: E402
from app.model import load_params, scope_for_competition             # noqa: E402

# 这些名字在 TEAM_ALIASES 里有条目（会被改写），同时它们的**原名**才是
# club 参数表里的正确写法 —— 正是当初出问题的那一批。
TRAPS = ["Bradford City", "Leicester City", "Wigan Athletic", "Peterborough United",
         "Plymouth Argyle", "Accrington Stanley", "Cheltenham Town", "Crewe Alexandra",
         "Colchester United", "Rotherham United", "Shrewsbury Town", "Grimsby Town"]


def _stub(payload):
    class R:
        headers = {"x-requests-remaining": "400", "x-requests-used": "100"}
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    odds_api.requests = types.SimpleNamespace(get=lambda *a, **k: R())


def in_club_table(name):
    known = set(load_params("club")["_index"].keys())
    return normalize_team_name(name).strip().lower() in known


def test_the_table_really_would_break_them():
    """先证明这张表确实会改坏这些名字，否则后面的测试是空的。"""
    broken = [n for n in TRAPS
              if n in odds_api.TEAM_ALIASES
              and in_club_table(n)
              and not in_club_table(odds_api.TEAM_ALIASES[n])]
    assert len(broken) >= 8, ("这批名字应该原名对得上、过表之后对不上", broken)
    print(f"✅ 前提成立：{len(broken)} 个队名原名在 club 表里、过 TEAM_ALIASES 之后就不在了")
    for n in broken[:3]:
        print(f"     {n}  →  {odds_api.TEAM_ALIASES[n]}")


def test_fetch_upcoming_events():
    odds_api.ODDS_API_KEY = "test"
    _stub([{"commence_time": "2026-08-22T15:00:00Z",
            "home_team": TRAPS[0], "away_team": TRAPS[1]}])

    got = odds_api.fetch_upcoming_events("leagueone")
    assert got[0]["team1"] == TRAPS[0], ("club 赛事不该套别名", got[0])
    assert in_club_table(got[0]["team1"]) and in_club_table(got[0]["team2"])

    got = odds_api.fetch_upcoming_events("efl_cup")
    assert got[0]["team1"] == odds_api.TEAM_ALIASES[TRAPS[0]], ("联赛杯该套别名", got[0])
    print("✅ fetch_upcoming_events：club 赛事保留原名，efl_cup 仍然转简称")


def test_fetch_h2h_odds():
    odds_api.ODDS_API_KEY = "test"
    book = {"key": "b", "markets": [{"key": "h2h", "outcomes": [
        {"name": TRAPS[0], "price": 2.5},
        {"name": TRAPS[1], "price": 2.9},
        {"name": "Draw", "price": 3.3}]}]}
    _stub([{"commence_time": "2026-08-22T15:00:00Z",
            "home_team": TRAPS[0], "away_team": TRAPS[1], "bookmakers": [book]}])

    rows, _ = odds_api.fetch_h2h_odds("leagueone")
    assert rows and rows[0]["team1"] == TRAPS[0], ("club 赛事不该套别名", rows)
    assert in_club_table(rows[0]["team1"]) and in_club_table(rows[0]["team2"])

    rows, _ = odds_api.fetch_h2h_odds("efl_cup")
    assert rows[0]["team1"] == odds_api.TEAM_ALIASES[TRAPS[0]], ("联赛杯该套别名", rows)
    print("✅ fetch_h2h_odds：同上（漏了这个的话市场赔率会一场都匹配不上）")


def test_fetch_recent_scores():
    odds_api.ODDS_API_KEY = "test"
    _stub([{"commence_time": "2026-08-01T15:00:00Z", "completed": True,
            "home_team": TRAPS[0], "away_team": TRAPS[1],
            "scores": [{"name": TRAPS[0], "score": "2"},
                       {"name": TRAPS[1], "score": "1"}]}])

    got = odds_api.fetch_recent_scores("leagueone")
    if got:
        assert got[0]["team1"] == TRAPS[0], ("club 赛事不该套别名", got[0])
        assert in_club_table(got[0]["team1"])
    got = odds_api.fetch_recent_scores("efl_cup")
    if got:
        assert got[0]["team1"] == odds_api.TEAM_ALIASES[TRAPS[0]], ("联赛杯该套别名", got[0])
    print("✅ fetch_recent_scores：同上")


def test_all_entry_points_covered():
    """源码级检查：凡是用到 TEAM_ALIASES 的地方都必须带 use_aliases 判断。

    加这条是因为第一次修漏了两个入口。以后再加新入口时，忘了判断会在这里
    直接失败，而不是等线上把队名改坏了才发现。
    """
    # 按**函数**判断，不是按行：真实写法里 use_aliases 常常在上一行的 if 上
    #   if use_aliases:
    #       h, a = TEAM_ALIASES.get(h, h), TEAM_ALIASES.get(a, a)
    # 逐行查会把这种正确写法误判成漏网（第一版就是这么误报的）。
    import ast
    path = os.path.join(HERE, "..", "app", "odds_api.py")
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    def uses(node, name):
        return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node))

    bad, checked = [], []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if not uses(fn, "TEAM_ALIASES"):
            continue
        checked.append(fn.name)
        if not uses(fn, "use_aliases"):
            bad.append(fn.name)
    assert checked, "一个用到 TEAM_ALIASES 的函数都没找到，检查本身失效了"
    assert not bad, (f"这些函数用了 TEAM_ALIASES 却没有按赛事判断: {bad}")
    print(f"✅ 源码检查：{len(checked)} 个函数用到 TEAM_ALIASES，"
          f"全部带 use_aliases 判断 → {checked}")


def main():
    test_the_table_really_would_break_them()
    test_fetch_upcoming_events()
    test_fetch_h2h_odds()
    test_fetch_recent_scores()
    test_all_entry_points_covered()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
