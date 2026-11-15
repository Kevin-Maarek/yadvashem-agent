import argparse
import asyncio
import logging
import signal
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError

from core.config import settings
from db.session import WORKER_LOCK, Session, advisory_lock
from services.ingestion.jobs import claim, finish, recover, validate_job
from db.models import WorkerHeartbeat
from core.observability import configure_logging
from services.ingestion.pipeline import run_stage
from core.errors import StageError
from prompts import validate_prompts

log = logging.getLogger(__name__)


def heartbeat():
    now = datetime.now(UTC)
    with Session.begin() as session:
        session.execute(insert(WorkerHeartbeat).values(id=1, last_seen=now)
                        .on_conflict_do_update(index_elements=["id"], set_={"last_seen": now}))


def health() -> bool:
    with Session() as session:
        current = session.get(WorkerHeartbeat, 1)
        return bool(current and datetime.now(UTC) - current.last_seen
                    < timedelta(seconds=2 * settings.model_timeout_seconds + 60))


async def stage(client, job, task):
    start = time.monotonic()
    failure = None
    try:
        validate_job(job)
        async with asyncio.timeout(2 * settings.model_timeout_seconds + 35):
            applied = await run_stage(client, job, task)
        finish(job.id, task, applied)
    except (StageError, TimeoutError) as exc:
        failure = exc if isinstance(exc, StageError) else StageError("stage_timeout", retry=True)
        finish(job.id, task, error=failure)
    except (DataError, IntegrityError):
        failure = StageError("database_record")
        finish(job.id, task, error=failure)
    except SQLAlchemyError:
        raise
    except Exception as exc:
        failure = StageError("stage_internal")
        log.error("stage_exception", extra={"metrics": {"type": type(exc).__name__, "stage": task.stage}})
        finish(job.id, task, error=failure)
    finally:
        log.info("ingestion_stage", extra={"metrics": {
            "job": job.id, "stage": task.stage, "photo_id": task.photo_id,
            "error_code": failure.code if failure else None, "ms": round((time.monotonic() - start) * 1000),
        }})


async def run():
    validate_prompts()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: stopping.set())
    timeout = httpx.Timeout(settings.model_timeout_seconds, connect=5)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        while not stopping.is_set():
            try:
                with advisory_lock(WORKER_LOCK) as worker:
                    if worker is not None:
                        recover()
                        last_heartbeat = 0.0
                        while not stopping.is_set():
                            worker.execute(text("SELECT 1"))
                            if time.monotonic() - last_heartbeat >= 5:
                                heartbeat()
                                last_heartbeat = time.monotonic()
                            work = claim()
                            if work is None:
                                await asyncio.sleep(0.3)
                                continue
                            job, task = work
                            if task.photo_id is None:
                                await stage(client, job, task)
                            else:
                                with advisory_lock(task.photo_id, photo=True) as photo:
                                    if photo is None:
                                        finish(job.id, task, error=StageError("photo_busy", retry=True))
                                    else:
                                        await stage(client, job, task)
            except SQLAlchemyError as exc:
                log.error("worker_database", extra={"metrics": {"type": type(exc).__name__}})
            if not stopping.is_set():
                await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    configure_logging()
    if args.health:
        raise SystemExit(0 if health() else 1)
    asyncio.run(run())
