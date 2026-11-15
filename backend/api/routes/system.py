from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text

from core.config import settings
from core.errors import StageError
from db.session import Session

router = APIRouter()


@router.get("/api/health")
def health():
    with Session() as session:
        types = session.scalars(text("""
            SELECT format_type(atttypid, atttypmod) FROM pg_attribute
            WHERE attrelid IN ('catalog_embeddings'::regclass, 'vision_embeddings'::regclass)
              AND attname = 'vector' AND NOT attisdropped
        """)).all()
        expected = f"vector({settings.embedding_dimensions})"
        if types != [expected, expected]:
            raise HTTPException(503, "embedding_schema_dimensions")
    return {"status": "ok"}



@router.get("/api/health/ollama", deprecated=True)
@router.get("/api/health/ai")
async def ai_health(request: Request):
    try:
        profiles = await request.app.state.models.profiles()
    except StageError as exc:
        return {"reachable": False, "ready": False, "models": {}, "error_code": exc.code,
                "provider": settings.ai_provider, "embedding_provider": settings.embedding_provider}
    models = {model: profiles.get(model) for model in {settings.chat_model, settings.vision_model,
                                                       settings.embedding_model}}
    return {"reachable": True, "ready": all(models.values()), "models": models,
            "expected_dimensions": settings.embedding_dimensions,
            "provider": settings.ai_provider, "embedding_provider": settings.embedding_provider}



@router.get("/api/config")
def configuration():
    return {"search_limit": settings.search_limit,
            "search_threshold": settings.search_threshold}
