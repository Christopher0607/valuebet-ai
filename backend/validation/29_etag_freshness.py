"""ETag 不能漏 —— /api/matches 和 /api/backtest-summary 的条件请求回归。

这个优化只有一种失败方式，而且是最坏的那种：数据变了但 ETag 没变，
于是用户永远看到旧数据，界面上还显示「已更新」。省流量是次要的，
**不漏**才是全部的重点，所以这个脚本几乎全在做同一件事——
把每一种可能改变返回值的写操作各做一遍，断言 ETag 必须跟着变。

反过来也要测：什么都不改的时候 ETag 必须**稳定**，否则 304 永远不命中，
优化等于没做（而且还白算一遍指纹）。

跑：cd backend && python3 validation/29_etag_freshness.py
"""
import os
import sys
import tempfile
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

DB = os.path.join(tempfile.mkdtemp(), "etag.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

import logging                                                  # noqa: E402
logging.disable(logging.INFO)

from fastapi.testclient import TestClient                       # noqa: E402
from app.models import (SessionLocal, engine, Base, Competition, Match,      # noqa: E402
                        Prediction, Odds, UpdateLog, MarketOdds)
from app.main import app                                        # noqa: E402

Base.metadata.drop_all(engine)
Base.metadata.create_all(engine)

db = SessionLocal()
comp = Competition(code="epl", name="English Premier League", name_zh="英超",
                   data_source="x", is_active=True)
comp2 = Competition(code="laliga", name="La Liga", name_zh="西甲",
                    data_source="x", is_active=True)
db.add_all([comp, comp2])
db.commit()

today = dt.date.today()
played = Match(competition_id=comp.id, date=today - dt.timedelta(days=7),
               team1="Arsenal", team2="Chelsea", score1=2, score2=1,
               status="played", time_utc="19:30")
future = Match(competition_id=comp.id, date=today + dt.timedelta(days=7),
               team1="Liverpool", team2="Everton", status="upcoming", time_utc="19:30")
db.add_all([played, future])
db.commit()
db.add(Prediction(match_id=played.id, prob_home=.5, prob_draw=.25, prob_away=.25,
                  predicted="home", is_correct=True, rps=0.12))
db.add(Prediction(match_id=future.id, prob_home=.5, prob_draw=.25, prob_away=.25,
                  predicted="home"))
db.add(UpdateLog(ran_at=dt.datetime.utcnow(), status="ok"))
db.commit()

client = TestClient(app)

failed = 0


def check(name, cond, extra=""):
    global failed
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f" —— {extra}" if extra else ""))
        failed += 1


def tag(path="/api/matches", **params):
    r = client.get(path, params=params)
    assert r.status_code == 200, r.status_code
    return r.headers["etag"]


def changes(name, mutate, path="/api/matches"):
    """做一次写操作，断言 ETag 必须变，并且旧 ETag 不再命中 304。"""
    before = tag(path)
    mutate()
    after = tag(path)
    check(f"{name} → ETag 变了", before != after,
          f"改动前后都是 {before}，客户端会一直拿到 304、永远看不到新数据")
    still = client.get(path, headers={"If-None-Match": before}).status_code == 304
    check(f"{name} → 旧 ETag 不再命中 304", not still)


print("ETag 新鲜度回归：任何会改变返回值的写操作都必须让 ETag 变\n")

print("【1】没有任何写操作时 ETag 必须稳定（否则 304 永远不命中）")
t1 = tag()
check("连续三次请求 ETag 一致", t1 == tag() == tag())
r = client.get("/api/matches", headers={"If-None-Match": t1})
check("带 If-None-Match 命中 304", r.status_code == 304, f"实际 {r.status_code}")
check("304 没有响应体（RFC 9110）", len(r.content) == 0, f"实际 {len(r.content)} 字节")
check("304 仍然回带 ETag", r.headers.get("etag") == t1)

print("\n【2】比赛/预测的各种改动")


def fill_score():
    m = db.query(Match).filter_by(id=future.id).one()
    m.score1, m.score2, m.status = 3, 0, "played"
    db.commit()


changes("把未来赛程填上比分（行数不变，只改内容）", fill_score)


def backfill_rps():
    p = db.query(Prediction).filter_by(match_id=future.id).one()
    p.rps, p.is_correct = 0.19, True
    db.commit()


changes("回填 rps（同样是原地修改）", backfill_rps)


def add_match():
    db.add(Match(competition_id=comp.id, date=today + dt.timedelta(days=14),
                 team1="Spurs", team2="Fulham", status="upcoming", time_utc="19:30"))
    db.commit()


changes("新增一场比赛", add_match)


def add_log():
    db.add(UpdateLog(ran_at=dt.datetime.utcnow(), status="ok", matches_updated=3))
    db.commit()


changes("跑了一轮更新（写 UpdateLog）", add_log)

print("\n【3】市场赔率")


def add_market():
    db.add(MarketOdds(match_id=played.id, n_books=8, best_home=2.2, best_draw=3.4,
                      best_away=3.6, avg_home=2.1, avg_draw=3.3, avg_away=3.4,
                      fetched_at=dt.datetime.utcnow()))
    db.commit()


changes("抓到市场报价", add_market)

print("\n【4】用户自己填的赔率（按账号隔离，不走 UpdateLog）")


def add_odds():
    db.add(Odds(match_id=played.id, odds_home=2.05, odds_draw=3.3, odds_away=3.7,
                recorded_at=dt.datetime.utcnow(), owner_id="local"))
    db.commit()


changes("本账号新增一条赔率", add_odds)

print("\n【5】赛事上下架会改变返回的比赛集合")


def deactivate():
    c = db.query(Competition).filter_by(id=comp2.id).one()
    c.is_active = False
    db.commit()


changes("停用一个赛事", deactivate)

print("\n【6】不同查询参数不能共用同一个 ETag")
t_all = tag()
t_up = tag(status_filter="upcoming")
t_comp = tag(competition_id=comp.id)
check("全量 vs ?status_filter=upcoming", t_all != t_up,
      "共用 ETag 的话，前端拿全量的 ETag 去问 upcoming 会拿到错的那份")
check("全量 vs ?competition_id=", t_all != t_comp)
check("upcoming vs competition_id", t_up != t_comp)
r = client.get("/api/matches", params={"status_filter": "upcoming"},
               headers={"If-None-Match": t_all})
check("拿全量 ETag 问 upcoming 不会误命中 304", r.status_code == 200, f"实际 {r.status_code}")

print("\n【7】backtest-summary 同样成立")
tb = tag("/api/backtest-summary")
check("连续两次一致", tb == tag("/api/backtest-summary"))
check("命中 304", client.get("/api/backtest-summary",
                             headers={"If-None-Match": tb}).status_code == 304)


def add_played():
    db.add(Match(competition_id=comp.id, date=today - dt.timedelta(days=1),
                 team1="Leeds", team2="Burnley", score1=1, score2=1,
                 status="played", time_utc="19:30"))
    db.commit()


changes("新增已赛比赛", add_played, path="/api/backtest-summary")
check("带 competition_id 的 ETag 跟不带的不同",
      tag("/api/backtest-summary") != tag("/api/backtest-summary", competition_id=comp.id))

print("\n【8】If-None-Match 的格式细节")
t = tag()
check("W/ 弱校验前缀也要命中（浏览器会自己加）",
      client.get("/api/matches", headers={"If-None-Match": "W/" + t}).status_code == 304)
check("多个候选值里有一个对就命中",
      client.get("/api/matches", headers={"If-None-Match": f'"other", {t}'}).status_code == 304)
check("* 命中", client.get("/api/matches", headers={"If-None-Match": "*"}).status_code == 304)
check("对不上就正常回 200",
      client.get("/api/matches", headers={"If-None-Match": '"nope"'}).status_code == 200)
check("完全不带这个头就正常回 200", client.get("/api/matches").status_code == 200)

print("\n【9】200 和 304 两条路必须给出同一份 ETag")
t = tag()
r304 = client.get("/api/matches", headers={"If-None-Match": t})
check("304 回带的 ETag 跟 200 的一致", r304.headers.get("etag") == t)
check("两条路都带 Cache-Control: no-cache",
      client.get("/api/matches").headers.get("cache-control") == "no-cache"
      and r304.headers.get("cache-control") == "no-cache")

print("\n【10】老库缺 updated_at 列时，启动补丁必须补上")
# 为什么单独测这个：predictions.updated_at 是这次新加的列，而线上那个
# Postgres 库是很早以前建的，里面**没有**这一列。create_all() 对已存在的表
# 什么都不做，靠的是 _SCHEMA_PATCHES 里的 ALTER TABLE。
# 漏了这一步的后果不是"慢"，是 /api/matches 每次都 500——算 ETag 的第一条
# 查询就会撞上 "column predictions.updated_at does not exist"，整个网站打不开。
# 这个项目已经因为同一件事踩过一次坑（f593ea5「新加的列从没真正到过 Postgres」），
# 所以这里用真的把列删掉再跑一遍补丁来验，不是看代码写没写。
from sqlalchemy import text as _text                              # noqa: E402
from app.models import engine as _engine, init_db as _init_db     # noqa: E402


def _cols():
    with _engine.begin() as conn:
        return {r[1] for r in conn.execute(_text("PRAGMA table_info(predictions)"))}


with _engine.begin() as conn:
    conn.execute(_text("ALTER TABLE predictions DROP COLUMN updated_at"))
check("先把列删掉，模拟老库", "updated_at" not in _cols())
# TestClient 默认把服务端异常直接抛出来（raise_server_exceptions=True），
# 不会变成 500 响应，所以这里用 try 捕获。
try:
    client.get("/api/matches")
    broke = False
except Exception as e:
    broke = "updated_at" in str(e)
check("老库上接口确实会挂（说明这一列是必需的）", broke,
      "没挂的话这条测试就没测到东西")
_init_db()
check("init_db 之后列回来了", "updated_at" in _cols(),
      "_SCHEMA_PATCHES 里漏了这一行，线上会整站 500")
check("接口恢复正常", client.get("/api/matches").status_code == 200)
db.expire_all()

print("\n【11】数据没变时正文必须逐字节一致")
a = client.get("/api/matches").content
b = client.get("/api/matches").content
check("同一个 ETag 下两次全量正文一致", a == b,
      "正文会变而 ETag 不变，说明指纹漏了某个字段")

db.close()
print("\n全部通过。" if failed == 0 else f"\n{failed} 项失败。")
sys.exit(0 if failed == 0 else 1)
