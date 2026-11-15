import asyncio
import logging
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from api.streaming import SSE_HEADERS, pack_event
from schemas.api import ChatRequest, ChatResult
from core.errors import StageError
from services.chat import conversations
from services.chat.runtime import run_turn

log = logging.getLogger(__name__)
router = APIRouter()


async def run_chat(request: Request, body: ChatRequest) -> ChatResult:
    return await run_turn(request.app.state.graph, body)


@router.post("/api/conversations")
async def create_conversation():
    return await asyncio.to_thread(conversations.create)


@router.post("/api/chat", response_model=ChatResult)
async def chat(body: ChatRequest, request: Request):
    return await run_chat(request, body)



@router.post("/api/chat/stream")
async def chat_stream(body: ChatRequest, request: Request):
    async def stream():
        work = asyncio.create_task(run_chat(request, body))
        try:
            yield pack_event("progress", {"status": "thinking"})
            while not work.done():
                done, _ = await asyncio.wait({work}, timeout=8)
                if not done:
                    yield ": heartbeat\n\n"
            result = work.result()
            for segment in result.text.splitlines(keepends=True):
                yield pack_event("text", {"delta": segment})
            yield pack_event("result", result)
        except Exception as exc:
            log.error("chat_error", extra={"metrics": {"type": type(exc).__name__}})
            yield pack_event("failure", {"code": exc.code if isinstance(exc, StageError) else "chat_error"})
        finally:
            if not work.done():
                work.cancel()
                with suppress(asyncio.CancelledError):
                    await work

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)

