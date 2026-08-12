"""
This is the piece that makes "every 12 hours, automatically" literally true —
as long as this backend process is running. APScheduler keeps a background
thread alive inside the same Python process as the API server.

Important honesty note (also in the README): this only runs while
`uvicorn` is running. If you close the terminal, stop Docker, or shut
down the machine, the schedule stops with it — because there is no
"it" running anywhere else. If you want it to survive your laptop being
closed, it needs to run on a server or an always-on machine (see README
"Running unattended" section for the lightweight options).
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import logging
from datetime import datetime

from .models import SessionLocal
from .updater import run_full_update

logger = logging.getLogger("valuebet.scheduler")
scheduler = BackgroundScheduler()


def scheduled_job():
    db = SessionLocal()
    try:
        result = run_full_update(db)
        logger.info(f"[scheduler] update finished: {result}")
    except Exception as e:
        logger.error(f"[scheduler] update failed: {e}")
    finally:
        db.close()


def start_scheduler(interval_hours: int = 12):
    scheduler.add_job(
        scheduled_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="update_job",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"[scheduler] started, will run every {interval_hours} hours")

    # 启动时立刻跑一次，好让新装的实例不用空等 12 小时。
    #
    # **但只在「上一次更新已经旧了」的时候跑。** 无条件跑会在 Render 免费档
    # 上变成灾难：免费实例闲置 15 分钟就休眠，用户每打开一次网站就是一次
    # 冷启动 = 一次全量更新。实测一次全量更新读写约 12,700 行（≈3 MB），
    # 一天开二十次就是 60 MB，一个月 1.8 GB——Supabase 免费档的 egress
    # 上限是 5 GB/月，真的被这么撑爆过（数据库本身只有 34 MB，却传出了
    # 5.28 GB，是库大小的 155 倍）。
    #
    # 12 小时那个定时任务照常，所以数据新鲜度不受影响：真正需要更新时
    # 它会跑；只是"因为有人访问所以重跑一遍"这件事没有意义。
    if _needs_startup_run(interval_hours):
        scheduler.add_job(scheduled_job, id="startup_run", replace_existing=True)
        logger.info("[scheduler] 上次更新已过期或从未更新过，启动时补跑一次")
    else:
        logger.info("[scheduler] 上次更新还新鲜，跳过启动时的全量更新（省 Supabase egress）")


def _needs_startup_run(interval_hours: int) -> bool:
    """上一次成功更新是不是已经旧到该重跑了。

    判断依据是 update_log 里最后一条的时间。读不到（表还没建、库连不上、
    第一次部署）一律当成"需要跑"——宁可多跑一次，也不要让一个新实例
    永远空着。
    """
    from .models import SessionLocal, UpdateLog
    db = SessionLocal()
    try:
        last = db.query(UpdateLog).order_by(UpdateLog.ran_at.desc()).first()
        if last is None or last.ran_at is None:
            return True
        age_h = (datetime.utcnow() - last.ran_at).total_seconds() / 3600.0
        logger.info("[scheduler] 上次更新在 %.1f 小时前", age_h)
        return age_h >= interval_hours
    except Exception as e:
        logger.warning("[scheduler] 读不到上次更新时间（%s），保险起见补跑一次", str(e)[:120])
        return True
    finally:
        db.close()


def next_run_info():
    job = scheduler.get_job("update_job")
    if not job:
        return None
    return job.next_run_time.isoformat() if job.next_run_time else None
