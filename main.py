import os
import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from database import init_db, SessionLocal, Reminder, engine
from scheduler import SchedulerManager

import websockets
import aiohttp

# Logging to stderr as required
logger = logging.getLogger("mcp_server")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

XIAOZHI_WS_URL = os.getenv("XIAOZHI_WS_URL", "wss://xiaozhi.example/ws")
CRON_API_URL = os.getenv("CRON_API_URL", "")
CRON_API_KEY = os.getenv("CRON_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

app = FastAPI(title="Xiaozhi MCP Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pydantic models for API
class CreateReminder(BaseModel):
    text: str = ""
    user_id: str = ""
    cron: str = ""  # cron expression OR leave empty and provide run_at
    run_at: str = ""  # ISO datetime string for one-time reminders

class ReminderOut(BaseModel):
    id: str = ""
    text: str = ""
    user_id: str = ""
    cron: str = ""
    run_at: str = ""
    active: bool = True

# Global objects filled on startup
scheduler_manager = None
xiaozhi_ws = None
xiaozhi_lock = asyncio.Lock()

async def xiaozhi_listener(ws_url: str, notifier):
    """Background task that maintains connection to Xiaozhi and receives messages."""
    backoff = 1
    while True:
        try:
            logger.info("Connecting to Xiaozhi websocket at %s", ws_url)
            async with websockets.connect(ws_url) as websocket:
                # Reset backoff after successful connect
                backoff = 1
                # Keep a reference for sending messages
                global xiaozhi_ws
                async with xiaozhi_lock:
                    xiaozhi_ws = websocket
                logger.info("Connected to Xiaozhi websocket")
                # Receive loop: we accept messages and log them (no auth required)
                async for message in websocket:
                    logger.info("Received message from Xiaozhi: %s", message)
                    # If Xiaozhi sends reminder creation messages in some format,
                    # you could parse them here and add into DB/scheduler.
                    # For now we just log incoming messages.
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Xiaozhi websocket error: %s", str(e))
            await asyncio.sleep(min(backoff, 60))
            backoff *= 2

async def send_to_xiaozhi(payload: dict):
    """Send a JSON payload back to Xiaozhi over the websocket if connected."""
    payload_text = json.dumps(payload)
    try:
        async with xiaozhi_lock:
            ws = xiaozhi_ws
        if ws is None:
            logger.warning("No Xiaozhi websocket connected. Cannot send message.")
            return "no_connection"
        await ws.send(payload_text)
        logger.info("Sent to Xiaozhi: %s", payload_text)
        return "sent"
    except Exception as e:
        logger.error("Failed to send to Xiaozhi: %s", str(e))
        return "error"

def notify_callback(reminder_id: str):
    """Callback used by scheduler when a reminder fires."""
    try:
        db = SessionLocal()
        r = db.query(Reminder).filter(Reminder.id == reminder_id).first()
        if not r:
            logger.error("Reminder not found at notify time: %s", reminder_id)
            return "not_found"
        payload = {
            "type": "reminder_fired",
            "id": r.id,
            "user_id": r.user_id,
            "text": r.text,
            "fired_at": datetime.utcnow().isoformat() + "Z",
        }
        # Send asynchronously to Xiaozhi via background task
        asyncio.create_task(send_to_xiaozhi(payload))
        logger.info("Notification callback queued for reminder %s", r.id)
        return "queued"
    except Exception as e:
        logger.error("notify_callback error: %s", str(e))
        return "error"

@app.on_event("startup")
async def startup_event():
    """App startup: initialize DB, scheduler, and Xiaozhi connector."""
    try:
        init_db(DATABASE_URL)
        global scheduler_manager
        scheduler_manager = SchedulerManager(trigger_callback=notify_callback, cron_api_url=CRON_API_URL, cron_api_key=CRON_API_KEY, database_url=DATABASE_URL)
        scheduler_manager.start()
        # load persisted reminders
        scheduler_manager.load_persisted_reminders()
        # start xiaozhi listener in background
        asyncio.create_task(xiaozhi_listener(XIAOZHI_WS_URL, notify_callback))
        logger.info("Startup complete")
    except Exception as e:
        logger.error("Startup error: %s", str(e))

@app.on_event("shutdown")
async def shutdown_event():
    """App shutdown: stop scheduler."""
    try:
        if scheduler_manager:
            scheduler_manager.shutdown()
        logger.info("Shutdown complete")
    except Exception as e:
        logger.error("Shutdown error: %s", str(e))

@app.post("/reminders", response_model=ReminderOut)
async def create_reminder(reminder: CreateReminder, background_tasks: BackgroundTasks):
    """Create a new reminder."""
    try:
        db = SessionLocal()
        rid = str(uuid.uuid4())
        run_at = None
        if reminder.run_at:
            try:
                run_at = datetime.fromisoformat(reminder.run_at)
            except Exception:
                raise HTTPException(status_code=400, detail="run_at must be ISO datetime")
        r = Reminder(id=rid, text=reminder.text, user_id=reminder.user_id or "", cron=reminder.cron or "", run_at=run_at, active=True, created_at=datetime.utcnow())
        db.add(r)
        db.commit()
        db.refresh(r)
        # schedule it
        added = scheduler_manager.add_reminder(r.id, r.text, r.user_id or "", r.cron or "", r.run_at)
        if not added:
            logger.error("Failed to schedule reminder %s", r.id)
        return {
            "id": r.id,
            "text": r.text,
            "user_id": r.user_id,
            "cron": r.cron or "",
            "run_at": r.run_at.isoformat() if r.run_at else "",
            "active": r.active,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("create_reminder error: %s", str(e))
        raise HTTPException(status_code=500, detail="Failed to create reminder")

@app.get("/reminders")
async def list_reminders():
    """List all reminders."""
    try:
        db = SessionLocal()
        rows = db.query(Reminder).all()
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "text": r.text,
                "user_id": r.user_id,
                "cron": r.cron or "",
                "run_at": r.run_at.isoformat() if r.run_at else "",
                "active": r.active,
            })
        return out
    except Exception as e:
        logger.error("list_reminders error: %s", str(e))
        raise HTTPException(status_code=500, detail="Failed to list reminders")

@app.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: str):
    """Delete a reminder."""
    try:
        db = SessionLocal()
        r = db.query(Reminder).filter(Reminder.id == reminder_id).first()
        if not r:
            raise HTTPException(status_code=404, detail="Reminder not found")
        r.active = False
        db.commit()
        scheduler_manager.remove_reminder(reminder_id)
        return {"status": "deleted", "id": reminder_id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("delete_reminder error: %s", str(e))
        raise HTTPException(status_code=500, detail="Failed to delete reminder")
