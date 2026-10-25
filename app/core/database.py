
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from app.core.config import settings

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=3,
    max_overflow=2,
    connect_args={
        "connect_timeout": 15,
        "hostaddr": "3.106.102.114",  # ใช้ทดสอบชั่วคราว
        "options": f"-c search_path={settings.db_schema},public",
    },
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    with SessionLocal() as db:
        yield db


def check_database():
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        return result.scalar_one()