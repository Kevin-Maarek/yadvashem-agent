from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)

WORKER_LOCK = 8011
GPU_LOCK = 8012
PHOTO_LOCK_NAMESPACE = 8013


@contextmanager
def advisory_lock(key: int, *, photo: bool = False):
    """Keep a session lock across external work without holding a transaction open."""
    connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    argument = f"{PHOTO_LOCK_NAMESPACE}, hashtext(:key)" if photo else ":key"
    values = {"key": str(key) if photo else key}
    acquired = False
    try:
        acquired = connection.scalar(text(f"SELECT pg_try_advisory_lock({argument})"), values)
        yield connection if acquired else None
    finally:
        try:
            if acquired and not connection.invalidated:
                connection.execute(text(f"SELECT pg_advisory_unlock({argument})"), values)
        finally:
            connection.close()
