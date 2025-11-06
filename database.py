import os
import logging
import sys
from datetime import datetime

from sqlalchemy import create_engine, Column, String, Boolean, DateTime, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Logging to stderr as required
logger = logging.getLogger("mcp_database")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

Base = declarative_base()
SessionLocal = None
engine = None

class Reminder(Base):
    """DB model for reminders."""
    __tablename__ = "reminders"
    id = Column(String(64), primary_key=True, index=True)
    text = Column(Text, nullable=False)
    user_id = Column(String(128), nullable=True)
    cron = Column(String(256), nullable=True)
    run_at = Column(DateTime, nullable=True)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

def init_db(database_url: str = ""):
    """Initialize database engine and session maker."""
    global SessionLocal, engine
    try:
        if database_url and database_url.strip() != "":
            db_url = database_url
        else:
            # fallback to sqlite file
            db_url = "sqlite:///./reminders.db"
        # Required for sqlite check_same_thread
        connect_args = {}
        if db_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        engine = create_engine(db_url, connect_args=connect_args, pool_pre_ping=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialized with %s", db_url)
    except Exception as e:
        logger.error("init_db error: %s", str(e))
        raise
