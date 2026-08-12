"""Supabase egress 优化 —— 两处改动的行为验证。

背景：Supabase 免费档 egress 上限 5 GB/月，实测被撑到 5.28 GB，而数据库
本身只有 34 MB——传出量是库大小的 155 倍，全是重复读。

量出来的两个来源：
  1. Render 免费档闲置 15 分钟就休眠，**每次访问都是冷启动**，而冷启动
     无条件跑一次全量更新（读写约 12,700 行 ≈ 3 MB）。一天开 20 次网站
     就是 20 次全量更新。
  2. 每次更新把**全部**预测重算一遍，包括六千多条早就定死的旧比赛。

对应两个修法：
  1. 启动时只在"上次更新已经旧了"才补跑（scheduler._needs_startup_run）
  2. 增量重算：跳过"已完赛 + 已有预测 + rps 已算出"的比赛；参数表指纹
     变了（= 重新训练过）才全量重算

第 2 点的风险是**静默不一致**：如果参数表变了却没重算，旧预测会跟新参数
对不上，而界面上完全看不出来。所以指纹检测必须真的有效，这里专门测。
"""
import sys
import os
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

from app import models, scheduler, updater                          # noqa: E402


def test_startup_gate():
    """冷启动不该每次都跑全量更新。"""
    db = models.SessionLocal()
    try:
        saved = db.query(models.UpdateLog).all()
        def scenario(hours_ago):
            db.query(models.UpdateLog).delete()
            if hours_ago is not None:
                db.add(models.UpdateLog(matches_updated=1, predictions_updated=1,
                                        bets_resolved=0, status="ok",
                                        ran_at=datetime.utcnow() - timedelta(hours=hours_ago)))
            db.commit()
            return scheduler._needs_startup_run(12)

        assert scenario(None) is True, "从没更新过必须跑"
        assert scenario(0.17) is False, "10 分钟前刚更新过，不该再跑"
        assert scenario(3) is False
        assert scenario(11.9) is False, "还没到 12 小时，不该跑"
        assert scenario(13) is True, "超过 12 小时该跑了"
        db.query(models.UpdateLog).delete()
        db.commit()
        print("✅ 启动补跑：只在「从没更新过」或「上次更新超过 12 小时」时触发")
        print("   一天开 20 次网站 → 全量更新从 20 次降到最多 2 次")
    finally:
        db.close()


def test_incremental_and_fingerprint():
    """增量重算省流量，但参数表一变必须全量重算。"""
    from sqlalchemy import event
    from app.models import engine
    rows = [0]

    @event.listens_for(engine, "after_cursor_execute")
    def _count(conn, cur, stmt, params, ctx, many):
        try:
            if cur.rowcount and cur.rowcount > 0:
                rows[0] += cur.rowcount
        except Exception:
            pass

    def run():
        db = models.SessionLocal()
        rows[0] = 0
        try:
            n = updater.update_predictions(db)
        finally:
            db.close()
        return rows[0], n

    total = models.SessionLocal()
    n_matches = total.query(models.Match).count()
    total.close()
    if n_matches < 100:
        print(f"⚠️ 库里只有 {n_matches} 场，样本太小，跳过增量校验")
        return

    # 先清掉指纹，让第一次跑是货真价实的全量——否则如果之前跑过，
    # "全量"那一次其实也是增量，两个数字一样，断言就成了空的。
    clr = models.SessionLocal()
    clr.query(models.AppState).filter_by(key="params_fingerprint").delete()
    clr.commit()
    clr.close()

    full_rows, full_n = run()          # 第一次：指纹对不上 → 全量
    inc_rows, inc_n = run()            # 第二次：参数没变 → 增量
    assert inc_rows < full_rows, ("增量应该读得更少", full_rows, inc_rows)
    assert inc_n < full_n, ("增量应该算得更少", full_n, inc_n)
    print(f"✅ 增量：读写 {full_rows:,} → {inc_rows:,} 行"
          f"（降低 {(1 - inc_rows / full_rows) * 100:.0f}%），"
          f"重算 {full_n:,} → {inc_n:,} 条")

    # 参数表一变就必须全量重算，否则旧预测跟新参数不一致且看不出来
    from app.model import _TRAINING_DIR
    path = os.path.join(_TRAINING_DIR, "fitted_parameters_club.json")
    before = os.stat(path).st_mtime
    os.utime(path, (time.time() + 1, time.time() + 1))
    try:
        again_rows, again_n = run()
        assert again_n == full_n, ("参数表变了必须全量重算", full_n, again_n)
        print(f"✅ 参数表 mtime 一变 → 重算回到 {again_n:,} 条（全量），"
              f"不会留下跟新参数不一致的旧预测")
    finally:
        os.utime(path, (before, before))
        run()          # 把指纹恢复成当前文件状态，不影响后续


def main():
    test_startup_gate()
    test_incremental_and_fingerprint()
    print("\n全部通过。")


if __name__ == "__main__":
    main()
