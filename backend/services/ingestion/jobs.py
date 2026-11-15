import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, delete, exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from integrations.ollama import prompt_for
from core.config import settings
from db.session import Session
from db.models import (Attempt, CatalogEmbedding, Image, Job, Record, SourceGate, Task,
                               VisionDescription, VisionEmbedding)
from core.errors import StageError
from integrations.yadvashem import normalize

TERMINAL = ("completed", "completed_with_errors")
READY = ("queued", "running")
STAGES = ("vision", "vision_embedding", "catalog", "save")
LIFECYCLE_LOCK = 8014


def add_task(session, job_id: int, stage: str, key: str, *, photo_id=None, image_id=None, **payload) -> bool:
    return session.execute(insert(Task).values(
        job_id=job_id, task_key=key, stage=stage, payload=payload, photo_id=photo_id, image_id=image_id,
    ).on_conflict_do_nothing(index_elements=["job_id", "task_key"]).returning(Task.id)).scalar_one_or_none() is not None


def validate_job(job: Job):
    if job.source != "live":
        raise StageError("source_kind")
    prompt_for()


def new_job(session, *, status="paused") -> Job:
    job = Job(source="live", scope={"source_client_id": str(uuid.uuid4())}, status=status)
    session.add(job)
    session.flush()
    add_task(session, job.id, "count", "count")
    add_task(session, job.id, "discovery", "page:1", page=1)
    return job


def current_job(session) -> Job:
    # Serialize initialization and lifecycle changes, including requests from different tabs.
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LIFECYCLE_LOCK})
    return session.scalar(select(Job).with_for_update()) or new_job(session)


def replace_job(session, job: Job) -> Job:
    """Called only after all in-flight work has settled. No old claim can write into the replacement."""
    session.execute(delete(Attempt).where(Attempt.task_id.in_(select(Task.id).where(Task.job_id == job.id))))
    session.execute(delete(Task).where(Task.job_id == job.id))
    session.delete(job)
    session.flush()
    for model in (VisionEmbedding, VisionDescription, CatalogEmbedding):
        session.execute(delete(model))
    session.execute(update(Image).values(status="pending"))
    # Reconstruct source-only fields, removing generated names, dates, places and lexical terms.
    for index, record in enumerate(session.scalars(select(Record).execution_options(yield_per=100)), 1):
        normalized, _ = normalize(record.source, record.photo_id, record.raw_search, record.raw_details)
        for key, value in normalized.items():
            setattr(record, key, value)
        if index % 100 == 0:
            session.flush()
    return new_job(session, status="queued")


def reconcile(session, job: Job) -> Job:
    if job.status in TERMINAL or job.status == "paused":
        return job
    if session.scalar(select(exists().where(Task.job_id == job.id, Task.status == "running"))):
        return job
    if job.status == "restarting":
        return replace_job(session, job)
    if job.status == "pausing":
        job.status = "paused"
    elif not session.scalar(select(exists().where(Task.job_id == job.id, Task.status == "queued"))):
        failures = session.scalar(select(exists().where(Task.job_id == job.id, Task.status == "failed")))
        job.status = "completed_with_errors" if failures else "completed"
        job.finished_at = datetime.now(UTC)
    return job


def control(job_id: int, action: str, *, acknowledge_access=False, confirm_erase=False) -> dict:
    with Session.begin() as session:
        job = current_job(session)
        if job.id != job_id:
            raise StageError("job_changed")
        if action == "restart":
            if not confirm_erase:
                raise StageError("erase_confirmation_required")
            if job.status != "restarting":
                job.status = "restarting"
        elif action == "pause" and job.status in (*READY, "pausing", "paused"):
            if job.status in READY:
                job.status = "pausing"
        elif action == "resume" and job.status in ("paused", "completed_with_errors", *READY):
            validate_job(job)
            gate = session.get(SourceGate, 1, with_for_update=True)
            if gate and gate.blocked:
                if not acknowledge_access:
                    raise StageError("source_forbidden")
                gate.blocked = False
            if job.status in ("paused", "completed_with_errors"):
                session.execute(update(Task).where(Task.job_id == job.id, Task.status.in_(("failed", "blocked")))
                                .values(status="queued", error_code=None, available_at=datetime.now(UTC)))
                job.finished_at = None
            if job.status in ("paused", "completed_with_errors"):
                job.status = "queued"
        else:
            raise StageError("job_transition")
        return job_header(reconcile(session, job))


def update_attempt(session, task: Task, status: str, error_code: str | None = None):
    attempt = session.scalar(select(Attempt).where(Attempt.task_id == task.id, Attempt.number == task.attempts))
    attempt.status = status
    attempt.finished_at = datetime.now(UTC)
    attempt.error_code = error_code


def block_saves(session, task: Task):
    """A failed prerequisite is one failure; its unrun save is only blocked."""
    scope = [Task.job_id == task.job_id, Task.photo_id == task.photo_id,
             Task.stage == "save", Task.status == "queued"]
    if task.stage != "catalog":
        scope.append(Task.image_id == task.image_id)
    session.execute(update(Task).where(*scope).values(status="blocked", error_code=None))


def recover():
    """Only the worker holding the process-wide advisory lock can recover old claims."""
    with Session.begin() as session:
        job = current_job(session)
        for task in session.scalars(select(Task).where(Task.job_id == job.id, Task.status == "running")):
            update_attempt(session, task, "interrupted", "worker_restart")
            task.status = "queued"
            task.claim_token = None
        session.flush()
        reconcile(session, job)


def claim() -> tuple[Job, Task] | None:
    with Session.begin() as session:
        job = current_job(session)
        if job.status not in READY:
            return None
        # Finish a photo before fetching another. Discovery is incremental and follows photo work.
        stage_order = case({"count": -1, "vision": 0, "vision_embedding": 1, "catalog": 2,
                            "save": 3, "missing_image": 4, "details": 5}, value=Task.stage, else_=6)
        while True:
            task = session.scalar(select(Task).where(
                Task.job_id == job.id, Task.status == "queued",
            ).order_by(Task.photo_id.asc().nulls_last(), stage_order, Task.id).limit(1).with_for_update(skip_locked=True))
            if task is None or task.available_at > datetime.now(UTC):
                return None
            if task.stage == "save":
                failed = session.scalar(select(Task).where(
                    Task.job_id == job.id, Task.photo_id == task.photo_id, Task.status == "failed",
                    Task.stage.in_(("vision", "vision_embedding", "catalog")),
                    (Task.image_id == task.image_id) | (Task.stage == "catalog"),
                ).limit(1))
                if failed:
                    block_saves(session, failed)
                    session.flush()
                    reconcile(session, job)
                    continue
            break
        if job.started_at is None:
            job.started_at = datetime.now(UTC)
        job.status = "running"
        task.status = "running"
        task.claim_token = str(uuid.uuid4())
        task.attempts += 1
        session.add(Attempt(task_id=task.id, number=task.attempts, status="running"))
        return job, task


def finish(job_id: int, claim_task: Task, apply=None, error: StageError | None = None):
    with Session.begin() as session:
        job = current_job(session)
        if job.id != job_id:
            return
        task = session.get(Task, claim_task.id, with_for_update=True)
        if task is None or task.status != "running" or task.claim_token != claim_task.claim_token:
            return
        task.claim_token = None
        task.error_code = error.code if error else None
        now = datetime.now(UTC)
        if job.status == "restarting":
            # Discard the result from before the user's reset request.
            task.status = "interrupted"
            update_attempt(session, task, "interrupted")
        elif error is None:
            outcome = apply(session, job, task) if apply else None
            task.status = outcome or "succeeded"
            update_attempt(session, task, task.status)
        elif error.code == "source_forbidden":
            job.status = "pausing"
            task.status = "queued"
            update_attempt(session, task, "blocked", error.code)
        elif error.code == "source_cooldown" and error.wait_until:
            task.status = "queued"
            task.available_at = error.wait_until
            update_attempt(session, task, "deferred", error.code)
        else:
            # Manual continuation gets a fresh bounded retry window, preserving all attempts.
            last_failure = session.scalar(select(func.coalesce(func.max(Attempt.number), 0)).where(
                Attempt.task_id == task.id, Attempt.status == "failed"))
            retries = session.scalar(select(func.count()).select_from(Attempt).where(
                Attempt.task_id == task.id, Attempt.status == "retrying", Attempt.number > last_failure))
            if error.retry and retries + 1 < settings.max_attempts:
                task.status = "queued"
                task.available_at = max(now + timedelta(seconds=settings.retry_base_seconds * 2**retries),
                                        error.wait_until or now)
                update_attempt(session, task, "retrying", error.code)
            else:
                task.status = "failed"
                update_attempt(session, task, "failed", error.code)
                if task.stage in ("vision", "vision_embedding", "catalog"):
                    block_saves(session, task)
                if task.image_id:
                    image = session.get(Image, task.image_id)
                    if image:
                        image.status = "failed"
        session.flush()
        reconcile(session, job)


def job_header(job: Job) -> dict:
    return {"id": job.id, "status": job.status, "created_at": job.created_at,
            "started_at": job.started_at, "finished_at": job.finished_at}


def collection_progress(session, job: Job) -> dict:
    completed = session.scalar(text("""
        SELECT count(*) FROM (
            SELECT photo_id FROM ingestion_tasks
            WHERE job_id = :job_id AND photo_id IS NOT NULL
            GROUP BY photo_id
            HAVING bool_and(status IN ('succeeded', 'skipped'))
               AND bool_or(stage = 'details' AND status = 'succeeded')
               AND count(*) FILTER (WHERE stage = 'save') > 0
               AND count(*) FILTER (WHERE stage = 'save') = count(*) FILTER (WHERE stage = 'vision')
        ) complete
    """), {"job_id": job.id})
    total = job.scope.get("search_reported_total", job.scope.get("reported_total"))
    percent = min(100, completed * 10_000 // total / 100) if total else 0 if total == 0 else None
    return {"completed_records": completed, "total_records": total, "percent": percent}


def photo_progress(session, job: Job, task: Task) -> dict:
    record = session.scalar(select(Record).where(Record.photo_id == task.photo_id, Record.source == "live"))
    image = session.get(Image, task.image_id) if task.image_id else session.scalar(
        select(Image).where(Image.record_id == record.id, Image.active).order_by(Image.position, Image.id).limit(1)
    ) if record else None
    tasks = session.scalars(select(Task).where(Task.job_id == job.id, Task.photo_id == task.photo_id)).all()
    stages = []
    for stage in STAGES:
        matched = next((item for item in tasks if item.stage == stage and
                        (stage == "catalog" or image and item.image_id == image.id)), None)
        status = matched.status if matched else "queued"
        attempt = session.scalar(select(Attempt).where(
            Attempt.task_id == matched.id, Attempt.number == matched.attempts,
        )) if matched and matched.attempts else None
        stages.append({"key": stage, "status": "completed" if status in ("succeeded", "skipped")
                       else "pending" if status in ("queued", "blocked", "interrupted") else status,
                       "reused": status == "skipped" or status == "succeeded" and matched.attempts == 0,
                       "started_at": attempt.started_at if attempt else None})
    return {"photo_id": task.photo_id, "image_id": image.id if image else None,
            "image_url": image.front_url if image else None, "back_url": image.back_url if image else None,
            "title": record.title if record else None, "stages": stages}


def snapshot(job_id: int | None = None) -> dict:
    with Session.begin() as session:
        job = current_job(session)
        if job_id is not None and job.id != job_id:
            raise StageError("job_changed")
        # Keep the most recently processed photo visible between stages and while paused.
        active = session.scalar(select(Task).where(Task.job_id == job.id, Task.status == "running"))
        latest = active if active and active.photo_id else session.scalar(
            select(Task).join(Attempt, Attempt.task_id == Task.id).where(
                Task.job_id == job.id, Task.photo_id.is_not(None)).order_by(Attempt.id.desc()).limit(1))
        current = photo_progress(session, job, latest) if latest else None
        # Saves can finish between browser polls. Retain their actual image and stage states
        # so the UI can acknowledge completion after the worker has moved on.
        saved = session.scalar(select(Task).join(Attempt, Attempt.task_id == Task.id).where(
            Task.job_id == job.id, Task.stage == "save", Task.status == "succeeded",
            Attempt.number == Task.attempts, Attempt.status == "succeeded",
        ).order_by(Attempt.id.desc()).limit(1))
        last_completed = {**photo_progress(session, job, saved), "save_task_id": saved.id} if saved else None
        failure = session.scalar(select(Task).where(Task.job_id == job.id, Task.status == "failed", Task.error_code.is_not(None))
                                 .order_by(Task.id.desc()).limit(1))
        gate = session.get(SourceGate, 1)
        failures = session.scalar(select(func.count(func.distinct(Task.photo_id))).where(
            Task.job_id == job.id, Task.status == "failed"))
        return {**job_header(job), "collection_progress": collection_progress(session, job),
                "access_blocked": bool(gate and gate.blocked), "failed_records": failures,
                "error_code": failure.error_code if failure else None,
                "activity": active.stage if active else None, "current": current,
                "last_completed": last_completed}
