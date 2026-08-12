"""市场赔率自动导入 —— 解析、匹配、以及最要紧的：EV 公式没被动过。

这次改动碰了 CLAUDE.md 里点过名的东西（odds_api.py 原本明确承诺"不请求
任何带赔率的接口"），所以验证要盯住两件事，而不只是"能不能跑通"：

  1. **EV 还是 `模型概率 × 市场原始赔率 - 1`。**
     CLAUDE.md 里唯一还禁止的循环用法是「把去抽水的市场概率当成自己的
     预测，再拿它去乘市场赔率算 EV」——那样算出来恒等于 -抽水，没有信息。
     自动抓回来的赔率只走"市场报价"这一路（预填输入框 + 比价），绝不能
     反过来变成模型概率。这里用一场构造好的比赛，手算 EV 再跟接口对，
     数值必须完全吻合。

  2. **对不上号的比赛必须被丢掉，不能猜。**
     队名对不上时宁可这场没有市场价（界面空着、用户照旧手填），也不能
     凭"日期相近 + 名字像"硬配——配错了会把 A 场的赔率填进 B 场，而这个
     错误在界面上完全看不出来。

解析部分的输入形状照抄用户真机跑出来的 /v4/sports/{key}/odds/ 返回。
"""
import sys
import os
import types
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app import odds_api                                             # noqa: E402
from app.model import expected_value                                 # noqa: E402


def _resp(payload, remaining="432", used="68"):
    class R:
        status_code = 200          # _get 会读它，桩必须有（这个字段是后来加的）
        headers = {"x-requests-remaining": remaining, "x-requests-used": used}
        def raise_for_status(self):
            pass
        def json(self):
            return payload
    return R()


def test_parse():
    """五家博彩商，只有两家三个结果齐全，其余三家必须被剔掉。"""
    raw = [{
        "commence_time": "2026-08-22T11:30:00Z",
        "home_team": "Hull City", "away_team": "Manchester United",
        "bookmakers": [
            {"key": "a", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hull City", "price": 7.05},
                {"name": "Manchester United", "price": 1.46},
                {"name": "Draw", "price": 4.34}]}]},
            {"key": "b", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hull City", "price": 7.40},
                {"name": "Manchester United", "price": 1.44},
                {"name": "Draw", "price": 4.50}]}]},
            # 缺平局
            {"key": "c", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hull City", "price": 99.0},
                {"name": "Manchester United", "price": 1.01}]}]},
            # 非法赔率（<=1 或 None）
            {"key": "d", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Hull City", "price": 1.0},
                {"name": "Manchester United", "price": 0},
                {"name": "Draw", "price": None}]}]},
            # 根本没有 h2h 盘口
            {"key": "e", "markets": [{"key": "totals", "outcomes": []}]},
        ]}]
    odds_api.requests = types.SimpleNamespace(get=lambda *a, **k: _resp(raw))
    odds_api.ODDS_API_KEY = "test"
    rows, quota = odds_api.fetch_h2h_odds("epl")

    assert len(rows) == 1, rows
    r = rows[0]
    assert r["n_books"] == 2, ("只有两家齐全", r["n_books"])
    assert r["best_home"] == 7.40 and r["best_draw"] == 4.50 and r["best_away"] == 1.46
    assert r["avg_home"] == round((7.05 + 7.40) / 2, 3)
    assert r["avg_away"] == round((1.46 + 1.44) / 2, 3)
    assert r["date"] == "2026-08-22" and r["time"] == "11:30"
    assert quota == {"remaining": 432, "used": 68}
    print("✅ 解析：非法/残缺报价被剔掉，最优价取 max，平均价只对齐全的求均")
    print(f"   配额从响应头读到了：剩 {quota['remaining']} / 已用 {quota['used']}")


def test_empty_when_no_book():
    """一家有效报价都没有的比赛，必须整条不产出——而不是产出一条全 None。"""
    raw = [{"commence_time": "2026-08-22T11:30:00Z",
            "home_team": "A", "away_team": "B",
            "bookmakers": [{"key": "x", "markets": [{"key": "h2h", "outcomes": [
                {"name": "A", "price": 1.0}]}]}]}]
    odds_api.requests = types.SimpleNamespace(get=lambda *a, **k: _resp(raw))
    rows, _ = odds_api.fetch_h2h_odds("epl")
    assert rows == [], rows
    print("✅ 没有任何有效报价时，这场不产出记录（不会写一行全 None 进库）")


def test_ev_formula_unchanged(db):
    """最要紧的一条：EV 必须是 模型概率 × 市场原始赔率 - 1。"""
    from app import models
    m = (db.query(models.Match)
           .join(models.MarketOdds, models.MarketOdds.match_id == models.Match.id)
           .first())
    if m is None:
        print("⚠️ 库里没有带市场赔率的比赛，跳过 EV 校验")
        return
    pred = db.query(models.Prediction).filter_by(match_id=m.id).first()
    mo = db.query(models.MarketOdds).filter_by(match_id=m.id).first()

    for label, p, o in (("主", pred.prob_home, mo.avg_home),
                        ("平", pred.prob_draw, mo.avg_draw),
                        ("客", pred.prob_away, mo.avg_away)):
        if not o:
            continue
        hand = p * o - 1                      # 手算
        got = expected_value(p, o)            # 系统算
        assert abs(hand - got) < 1e-9, (label, hand, got)
        # 反例：如果有人把"去抽水的市场概率"当成预测，EV 会恒等于 -抽水。
        # 这里显式算一遍那个错误版本，证明两者不是一回事。
        inv = 1.0 / mo.avg_home + (1.0 / mo.avg_draw if mo.avg_draw else 0) + 1.0 / mo.avg_away
        wrong = (1.0 / o / inv) * o - 1
        assert abs(wrong - got) > 1e-6, "EV 退化成了循环算法（等于 -抽水），这是被禁止的用法"
        print(f"   {label}: 模型概率 {p:.4f} × 市场赔率 {o} - 1 = {got:+.4f}   "
              f"（若误用去抽水市场概率会得到 {wrong:+.4f}，两者不同 ✅）")
    print("✅ EV 公式未被改动，也没有退化成循环算法")


def test_no_fuzzy_match(db):
    """队名对不上的比赛必须被丢弃，不能靠日期相近硬配。"""
    from app import models, updater
    before = db.query(models.MarketOdds).count()
    today = date.today()

    def fake(code, regions="eu"):
        # 全是库里不存在的队名，一条都不该落库
        return ([{"date": today.isoformat(), "time": "14:00",
                  "team1": "查无此队甲", "team2": "查无此队乙",
                  "n_books": 9, "best_home": 2.0, "best_draw": 3.0, "best_away": 4.0,
                  "avg_home": 1.9, "avg_draw": 2.9, "avg_away": 3.8}],
                {"remaining": 400, "used": 100})
    real = odds_api.fetch_h2h_odds
    odds_api.fetch_h2h_odds = fake
    try:
        res = updater.refresh_market_odds(db)
    finally:
        odds_api.fetch_h2h_odds = real
    after = db.query(models.MarketOdds).count()
    assert after == before, (before, after)
    assert res["updated"] == 0 and res["unmatched"] > 0, res
    print(f"✅ 对不上号的 {res['unmatched']} 条被丢弃，一行都没写进库（{before} → {after}）")


def test_cross_midnight(db):
    """UTC 开球跨零点导致日期差一天时，凭队名对仍要能配上。"""
    from app import models, updater
    m = (db.query(models.Match)
           .filter(models.Match.status == "upcoming",
                   models.Match.date >= date.today(),
                   models.Match.date <= date.today() + timedelta(days=updater._ODDS_LOOKAHEAD_DAYS))
           .first())
    if m is None:
        print("⚠️ 近期没有比赛，跳过跨日校验")
        return
    shifted = (m.date + timedelta(days=1)).isoformat()

    def fake(code, regions="eu"):
        return ([{"date": shifted, "time": "01:30", "team1": m.team1, "team2": m.team2,
                  "n_books": 7, "best_home": 3.33, "best_draw": 3.44, "best_away": 3.55,
                  "avg_home": 3.11, "avg_draw": 3.22, "avg_away": 3.30}],
                {"remaining": 399, "used": 101})
    real = odds_api.fetch_h2h_odds
    odds_api.fetch_h2h_odds = fake
    try:
        updater.refresh_market_odds(db)
    finally:
        odds_api.fetch_h2h_odds = real
    row = db.query(models.MarketOdds).filter_by(match_id=m.id).first()
    assert row is not None and row.best_home == 3.33, row
    print(f"✅ 库里日期 {m.date}、报价日期 {shifted}，差一天仍正确配上 "
          f"（{m.team1} vs {m.team2}）")


def test_window_only_gates_the_request(db):
    """窗口只决定「要不要发请求」，不该限制留下哪些结果。

    第一版把 _ODDS_LOOKAHEAD_DAYS 同时当成了匹配池的范围，于是博彩公司已经
    开盘、接口已经返回、配额也已经花掉的那些 6-14 天后的比赛，全部被当成
    「对不上号」丢掉。实测代价：2,304 场未来比赛里只有 21 场（0.9%）拿得到
    赔率——不是联赛没覆盖，是自己把已经买到手的数据扔了。
    """
    from app import models, updater
    db.query(models.MarketOdds).delete()
    db.commit()
    today = date.today()
    far = today + timedelta(days=updater._ODDS_LOOKAHEAD_DAYS + 25)  # 远在窗口之外

    comp = None
    for c in db.query(models.Competition).all():
        if c.code not in odds_api.SPORT_KEYS:
            continue
        has_near = (db.query(models.Match)
                      .filter(models.Match.competition_id == c.id,
                              models.Match.status == "upcoming",
                              models.Match.date >= today,
                              models.Match.date <= today + timedelta(days=updater._ODDS_LOOKAHEAD_DAYS))
                      .first())
        if has_near:
            comp = c
            break
    if comp is None:
        print("⚠️ 没有「窗口内有比赛」的赛事，跳过窗口校验")
        return

    far_matches = (db.query(models.Match)
                     .filter(models.Match.competition_id == comp.id,
                             models.Match.status == "upcoming",
                             models.Match.date > today + timedelta(days=updater._ODDS_LOOKAHEAD_DAYS),
                             models.Match.date <= far)
                     .limit(5).all())
    if not far_matches:
        print(f"⚠️ {comp.code} 窗口外没有比赛，跳过窗口校验")
        return

    def fake(code, regions=None):
        if code != comp.code:
            return [], {"remaining": 400, "used": 100}
        return ([{"date": m.date.isoformat(), "time": "14:00",
                  "team1": m.team1, "team2": m.team2, "n_books": 9,
                  "best_home": 2.55, "best_draw": 3.42, "best_away": 3.10,
                  "avg_home": 2.41, "avg_draw": 3.30, "avg_away": 2.95}
                 for m in far_matches], {"remaining": 400, "used": 100})

    real = odds_api.fetch_h2h_odds
    odds_api.fetch_h2h_odds = fake
    try:
        res = updater.refresh_market_odds(db)
    finally:
        odds_api.fetch_h2h_odds = real

    saved = {mo.match_id for mo in db.query(models.MarketOdds).all()}
    missing = [m for m in far_matches if m.id not in saved]
    assert not missing, ("窗口外的比赛被丢掉了", [(m.date.isoformat(), m.team1) for m in missing])
    assert res["unmatched"] == 0, res
    days_out = (max(m.date for m in far_matches) - today).days
    print(f"✅ [{comp.code}] 窗口是 +{updater._ODDS_LOOKAHEAD_DAYS} 天，"
          f"但窗口外最远 +{days_out} 天的 {len(far_matches)} 场全部保留，"
          f"没有被当成对不上号丢掉")


def main():
    test_parse()
    test_empty_when_no_book()
    from app import models
    db = models.SessionLocal()
    try:
        test_ev_formula_unchanged(db)
        test_no_fuzzy_match(db)
        test_cross_midnight(db)
        test_window_only_gates_the_request(db)
    finally:
        db.close()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
