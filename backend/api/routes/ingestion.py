from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from core.config import settings
from core.errors import StageError
from schemas.api import Action, JobAction
from services.ingestion.jobs import control, snapshot

router = APIRouter()


@router.get("/api/ingestion")
def get_ingestion():
    return snapshot()


@router.post("/api/ingestion/{action}")
async def update_ingestion(action: Action, body: JobAction, request: Request):
    if action in ("resume", "restart"):
        if action == "restart" and not body.confirm_erase:
            raise StageError("erase_confirmation_required")
        profiles = await request.app.state.models.profiles()
        if not all(model in profiles for model in (settings.vision_model, settings.embedding_model)):
            raise StageError("ai_model_missing" if settings.ai_provider != "ollama"
                             or settings.embedding_provider != "ollama" else "ollama_model_missing")
        await request.app.state.models.embed(
            "בדיקת ממדי וקטור לפני קליטה חיה", interactive=True,
            model_digest=profiles[settings.embedding_model],
        )
    await run_in_threadpool(control, body.job_id, action,
                            acknowledge_access=body.acknowledge_access, confirm_erase=body.confirm_erase)
    return await run_in_threadpool(snapshot)
