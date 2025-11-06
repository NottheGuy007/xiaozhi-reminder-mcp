import os
import logging
import sys
from datetime import datetime
import requests
import threading
import traceback

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from database import SessionLocal, Reminder

# Logging to stderr as required
logger = logging.getLogger("mcp_scheduler")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

CRON_API_URL = os.getenv("CRON_API_URL", "")
CRON_API_KEY = os.getenv("CRON_API_KEY", "")

class SchedulerManager:
    """Manages APScheduler jobs and optional external cron API registration."""
    def __init__(self, trigger_callback, cron_api_url: str = "", cron_api_key: str = "", database_url: str = ""):
        """Create scheduler manager."""
        self.trigger_callback = trigger_callback
        self.cron_api_url = cron_api_url or CRON_API_URL
        self.cron_api_key = cron_api_key or CRON_API_KEY
        self.database_url = database_url
        self.scheduler = BackgroundScheduler()
        self.lock = threading.Lock()

    def start(self):
        """Start the scheduler."""
        self.scheduler.start(paused=False)
        logger.info("Scheduler started")

    def shutdown(self):
        """Shutdown the scheduler."""
        try:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler shutdown")
        except Exception as e:
            logger.error("Error shutting down scheduler: %s", str(e))

    def _job_wrapper(self, reminder_id: str):
        """Internal wrapper that calls the user-supplied trigger callback and marks one-shot reminders inactive."""
        try:
            logger.info("Job fired for reminder %s", reminder_id)
            self.trigger_callback(reminder_id)
            # For one-time reminders, mark inactive in DB
            try:
                db = SessionLocal()
                r = db.query(Reminder).filter(Reminder.id == reminder_id).first()
                if r and (r.cron is None or r.cron == ""):
                    r.active = False
                    db.commit()
                    logger.info("Marked one-time reminder inactive: %s", reminder_id)
            except Exception as e:
                logger.error("Error marking reminder inactive: %s", str(e))
        except Exception:
            traceback.print_exc()
            logger.error("Error in job wrapper for %s", reminder_id)

    def add_reminder(self, reminder_id: str, text: str, user_id: str, cron_expr: str = "", run_at=None):
        """Schedule a reminder by cron expression or exact datetime."""
        try:
            with self.lock:
                job_id = f"reminder_{reminder_id}"
                # Remove existing
                try:
                    self.scheduler.remove_job(job_id)
                except Exception:
                    pass
                if cron_expr:
                    # Schedule locally
                    try:
                        trigger = CronTrigger.from_crontab(cron_expr)
                        self.scheduler.add_job(self._job_wrapper, trigger=trigger, args=[reminder_id], id=job_id, replace_existing=True)
                        logger.info("Scheduled cron reminder %s locally: %s", reminder_id, cron_expr)
                        # Optionally register with external cron API
                        if self.cron_api_url and self.cron_api_key:
                            self._register_external_cron(reminder_id, cron_expr, text, user_id)
                        return True
                    except Exception as e:
                        logger.error("Invalid cron expression: %s", str(e))
                        return False
                elif run_at:
                    # run_at is a datetime
                    try:
                        trigger = DateTrigger(run_date=run_at)
                        self.scheduler.add_job(self._job_wrapper, trigger=trigger, args=[reminder_id], id=job_id, replace_existing=True)
                        logger.info("Scheduled one-time reminder %s at %s", reminder_id, run_at.isoformat())
                        return True
                    except Exception as e:
                        logger.error("Failed to schedule one-time reminder: %s", str(e))
                        return False
                else:
                    logger.error("Neither cron_expr nor run_at provided for reminder %s", reminder_id)
                    return False
        except Exception as e:
            logger.error("add_reminder error: %s", str(e))
            return False

    def remove_reminder(self, reminder_id: str):
        """Remove scheduled reminder."""
        try:
            with self.lock:
                job_id = f"reminder_{reminder_id}"
                try:
                    self.scheduler.remove_job(job_id)
                    logger.info("Removed job %s", job_id)
                except Exception:
                    logger.info("Job %s not present locally", job_id)
                # Optionally unregister from external cron API if supported
                if self.cron_api_url and self.cron_api_key:
                    try:
                        resp = requests.delete(f"{self.cron_api_url}/jobs/{reminder_id}", headers={"Authorization": f"Bearer {self.cron_api_key}"}, timeout=10)
                        logger.info("External cron unregister status %s for %s", resp.status_code, reminder_id)
                    except Exception as e:
                        logger.error("Failed to unregister external cron: %s", str(e))
        except Exception as e:
            logger.error("remove_reminder error: %s", str(e))

    def _register_external_cron(self, reminder_id: str, cron_expr: str, text: str, user_id: str):
        """Attempt to register the cron with an external cron API using CRON_API_KEY."""
        try:
            payload = {"id": reminder_id, "cron": cron_expr, "text": text, "user_id": user_id}
            headers = {"Authorization": f"Bearer {self.cron_api_key}", "Content-Type": "application/json"}
            logger.info("Registering external cron for %s", reminder_id)
            resp = requests.post(f"{self.cron_api_url}/jobs", json=payload, headers=headers, timeout=10)
            if resp.status_code not in (200, 201):
                logger.error("External cron registration failed %s: %s", resp.status_code, resp.text)
            else:
                logger.info("External cron registered for %s", reminder_id)
        except Exception as e:
            logger.error("External cron registration exception: %s", str(e))

    def load_persisted_reminders(self):
        """Load active reminders from the DB and schedule them locally."""
        try:
            db = SessionLocal()
            rows = db.query(Reminder).filter(Reminder.active == True).all()
            logger.info("Loading %d persisted reminders", len(rows))
            for r in rows:
                try:
                    self.add_reminder(r.id, r.text, r.user_id or "", r.cron or "", r.run_at)
                except Exception as e:
                    logger.error("Failed to schedule persisted reminder %s: %s", r.id, str(e))
        except Exception as e:
            logger.error("load_persisted_reminders error: %s", str(e))
