"""串关页 "Failed to fetch" —— 复现原因，并验证批量接口修好了它。

现场：用户在串关页选了 115 场，点「生成推荐组合」，前端直接报
"Failed to fetch"（不是某个 HTTP 错误码，是 fetch 本身失败）。

起因是我自己引入的。接进市场赔率自动导入之前，串关候选池只收「用户手填过
赔率」的比赛，撑死十几场；接进来之后每场都有市场价，用户真的能选 115 场。
而前端在搜索之前会把选中比赛的赔率存回后端，写法是**每场一个 POST**：

    await Promise.all(matches.map(m => api("/odds", {method:"POST", ...})))

115 场就是 115 个并发请求。浏览器对同一域名并发上限约 6 条，要排 19 批；
云端连接池 pool_size=5 + max_overflow=5 只有 10 条，请求在服务端还要抢连接；
Render 免费档的请求超时一到就切断连接 —— 浏览器看到的就是 Failed to fetch。

这个脚本量的是**服务端这一侧的真实代价**（请求数、SQL 次数、写事务次数、
耗时），证明差距是数量级的，而不是"感觉快了点"。浏览器并发上限和 Render
超时没法在这里复现，但它们只会让实际情况比这里更糟。
"""
import sys
import os
import time
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from sqlalchemy import event                                          # noqa: E402
from app import models                                                # noqa: E402
from app.models import engine                                         # noqa: E402
from app.main import (submit_odds, submit_odds_bulk,                  # noqa: E402
                      OddsInput, OddsBulkInput)

_sql = []


@event.listens_for(engine, "before_cursor_execute")
def _count(conn, cur, stmt, params, ctx, many):
    _sql.append(stmt.strip().split()[0].upper())


def pick(db, n):
    return (db.query(models.Match)
              .filter(models.Match.status == "upcoming", models.Match.date >= date.today())
              .limit(n).all())


def main():
    # 每条路径用**独立的 session**，跟生产一致：FastAPI 的 get_db 每个请求
    # 都给一个新 session。共用一个 session 测的话，前一条路径遗留的 ORM
    # 对象会在后一条路径的 autoflush 里被逐个刷新，凭空多出上百条 SELECT，
    # 量出来的数字不是接口本身的代价。第一次这么写就被这个坑到了。
    db = models.SessionLocal()
    try:
        ms = pick(db, 115)
        n = len(ms)
    finally:
        db.close()
    if n < 10:
        print(f"⚠️ 库里近期比赛只有 {n} 场，样本太小，结论不可靠")
        return
    ids = [m.id for m in ms]
    print(f"用库里 {n} 场真实的未来比赛测（用户现场选的是 115 场）\n")

    # ── 旧写法：每场一个 POST /api/odds ──────────────────────
    db = models.SessionLocal()
    try:
        _sql.clear()
        t = time.time()
        for mid in ids:
            submit_odds(OddsInput(match_id=mid, odds_home=2.40, odds_draw=3.50, odds_away=3.00),
                        db=db, user=None)
        old_t = time.time() - t
        old_sql = list(_sql)
    finally:
        db.close()

    # ── 新写法：一个 POST /api/odds/bulk ─────────────────────
    db = models.SessionLocal()
    try:
        _sql.clear()
        t = time.time()
        res = submit_odds_bulk(
            OddsBulkInput(items=[OddsInput(match_id=mid, odds_home=2.40,
                                           odds_draw=3.50, odds_away=3.00) for mid in ids]),
            db=db, user=None)
        new_t = time.time() - t
        new_sql = list(_sql)
    finally:
        db.close()

    db = models.SessionLocal()
    try:

        def commits(sqls):
            return sum(1 for s in sqls if s in ("COMMIT", "INSERT"))

        print(f"{'':16s} {'HTTP 请求':>9s} {'SQL 次数':>9s} {'INSERT/COMMIT':>14s} {'服务端耗时':>10s}")
        print(f"{'旧（每场一个）':16s} {n:9d} {len(old_sql):9d} {commits(old_sql):14d} {old_t*1000:9.0f}ms")
        print(f"{'新（一次批量）':16s} {1:9d} {len(new_sql):9d} {commits(new_sql):14d} {new_t*1000:9.0f}ms")
        print(f"\n批量接口返回: {res}")

        assert res["saved"] == n, res
        assert len(new_sql) < len(old_sql) / 10, ("SQL 次数应该降一个数量级", len(old_sql), len(new_sql))
        print(f"\n✅ 请求数 {n} → 1，SQL {len(old_sql)} → {len(new_sql)}，"
              f"写事务 {commits(old_sql)} → {commits(new_sql)}")

        # ── 不存在的 match_id 必须被跳过，不能整批失败 ──────────
        _sql.clear()
        res2 = submit_odds_bulk(
            OddsBulkInput(items=[
                OddsInput(match_id=ids[0], odds_home=2.0, odds_draw=3.0, odds_away=4.0),
                OddsInput(match_id=999999999, odds_home=2.0, odds_draw=3.0, odds_away=4.0),
            ]), db=db, user=None)
        assert res2 == {"saved": 1, "skipped": 1}, res2
        print(f"✅ 混进一个不存在的 match_id：{res2}，好的那条照存，整批不失败")

        # ── 空列表不该炸，也不该白跑一个事务 ────────────────────
        assert submit_odds_bulk(OddsBulkInput(items=[]), db=db, user=None) == {"saved": 0, "skipped": 0}
        print("✅ 空列表直接返回，不开事务")

        print("\n全部通过。")
    finally:
        db.close()


if __name__ == "__main__":
    main()
