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

    # Run once immediately in the background so the dashboard isn't empty
    # for 12 hours after a fresh install. This does NOT reset the 12-hour
    # countdown above — it's a one-off extra run on top of it.
    scheduler.add_job(scheduled_job, id="startup_run", replace_existing=True)


def next_run_info():
    job = scheduler.get_job("update_job")
    if not job:
        return None
    return job.next_run_time.isoformat() if job.next_run_time else None
