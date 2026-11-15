from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy.exc import SQLAlchemyError

from api.errors import database_error_handler, stage_error_handler
from api.routes import chat, ingestion, system
from core.config import settings
from core.errors import StageError
from core.observability import configure_logging
from integrations.models import create_models
from integrations.names import Names
from prompts import validate_prompts
from services.chat.agent import build_graph


@asynccontextmanager
async def lifespan(app):
    configure_logging()
    validate_prompts()
    async with httpx.AsyncClient(timeout=httpx.Timeout(settings.model_timeout_seconds, connect=5),
                                 trust_env=False) as client:
        app.state.models = create_models(client)
        app.state.graph = build_graph(app.state.models, Names(client))
        yield



app = FastAPI(title="Yad Vashem archival search", lifespan=lifespan)
app.add_exception_handler(StageError, stage_error_handler)
app.add_exception_handler(SQLAlchemyError, database_error_handler)
app.include_router(system.router)
app.include_router(ingestion.router)
app.include_router(chat.router)
