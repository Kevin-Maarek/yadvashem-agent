import json
import logging

from fastapi import Request
from fastapi.responses import Response
from sqlalchemy.exc import SQLAlchemyError

from core.errors import StageError

log = logging.getLogger(__name__)


async def stage_error_handler(request: Request, error: StageError):
    codes = {"conversation_missing": 404, "source_forbidden": 403,
             "ollama_unavailable": 503, "ollama_model_missing": 503, "production_prompt_missing": 409,
             "ai_unavailable": 503, "ai_model_missing": 503, "ai_auth": 503,
             "ai_rate_limit": 429, "ai_timeout": 504, "ai_server": 503}
    return Response(json.dumps({"detail": error.code}), status_code=codes.get(error.code, 409),
                    media_type="application/json")



async def database_error_handler(request: Request, error: SQLAlchemyError):
    log.error("api_database", extra={"metrics": {"type": type(error).__name__}})
    return Response('{"detail":"database_unavailable"}', status_code=503, media_type="application/json")

