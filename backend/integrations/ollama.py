import asyncio
import base64
import hashlib
import logging
import math
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import TypeVar

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select

from core.config import settings
from domain.catalog import PhotoEnrichment
from db.session import GPU_LOCK, Session, advisory_lock
from db.models import ChatWaiter
from core.errors import StageError
from core.hashing import fingerprint
from prompts import load_prompt
from integrations.model_common import VisionModel

MAX_PIXELS = 24_000_000
MAX_DIMENSION = 8_000
MAX_JPEG_BYTES = 10 * 1024 * 1024
FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP", "image/tiff": "TIFF"}
log = logging.getLogger(__name__)
Schema = TypeVar("Schema", bound=BaseModel)


def prompt_for() -> tuple[str, str]:
    filename = "vision.production.txt"
    try:
        prompt = load_prompt(filename)
        recovery = load_prompt("vision.recovery.txt")
    except RuntimeError as exc:
        raise StageError("production_prompt_missing") from exc
    version = fingerprint(filename, prompt, recovery, PhotoEnrichment.model_json_schema(),
                          "jpeg-exif-rgb-q95-444", "front-then-back-v1",
                          settings.vision_output_tokens, min(settings.vision_output_tokens, 2600),
                          16384, False, 0)
    return prompt, version


def prepare_image(raw: bytes, media_type: str) -> tuple[str, str]:
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format != FORMATS.get(media_type) or getattr(image, "is_animated", False):
                raise StageError("image_format")
            width, height = image.size
            if max(width, height) > MAX_DIMENSION or width * height > MAX_PIXELS:
                raise StageError("image_dimensions")
            with ImageOps.exif_transpose(image) as upright:
                with upright.convert("RGB") as rgb, BytesIO() as buffer:
                    rgb.save(buffer, "JPEG", quality=95, subsampling=0)
                    if buffer.tell() > MAX_JPEG_BYTES:
                        raise StageError("image_payload")
                    return base64.b64encode(buffer.getbuffer()).decode("ascii"), hashlib.sha256(raw).hexdigest()
    except Image.DecompressionBombError as exc:
        raise StageError("image_dimensions") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise StageError("image_format") from exc


@asynccontextmanager
async def gpu_slot(*, interactive: bool):
    token = str(uuid.uuid4()) if interactive else None
    deadline = time.monotonic() + settings.ollama_timeout_seconds + 10
    if token:
        with Session.begin() as session:
            session.add(ChatWaiter(id=token, expires_at=datetime.now(UTC)
                                   + timedelta(seconds=settings.ollama_timeout_seconds + 15)))
    try:
        while time.monotonic() < deadline:
            with advisory_lock(GPU_LOCK) as lock:
                if lock is not None:
                    with Session.begin() as session:
                        session.execute(delete(ChatWaiter).where(ChatWaiter.expires_at <= datetime.now(UTC)))
                        waiting = session.scalar(select(ChatWaiter.id).limit(1))
                        if token:
                            waiter = session.get(ChatWaiter, token)
                            if waiter is not None:
                                session.delete(waiter)
                    if interactive or not waiting:
                        yield
                        return
            await asyncio.sleep(0.2)
        raise StageError("ollama_busy", retry=True)
    finally:
        if token:
            with Session.begin() as session:
                session.execute(delete(ChatWaiter).where(ChatWaiter.id == token))


class Ollama(VisionModel):
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.base = settings.ollama_url.rstrip("/")

    async def profiles(self) -> dict[str, str]:
        try:
            response = await self.client.get(f"{self.base}/api/tags", timeout=5)
            response.raise_for_status()
            tags = response.json()["models"]
            return {item["name"]: item["digest"] for item in tags
                    if isinstance(item.get("name"), str) and isinstance(item.get("digest"), str)}
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError) as exc:
            raise StageError("ollama_unavailable", retry=True) from exc

    async def digest(self, model: str) -> str:
        value = (await self.profiles()).get(model)
        if not value:
            raise StageError("ollama_model_missing")
        return value

    async def request(self, endpoint: str, body: dict, *, interactive: bool) -> dict:
        started = time.monotonic()
        status = 0
        try:
            async with gpu_slot(interactive=interactive):
                async with asyncio.timeout(settings.ollama_timeout_seconds):
                    response = await self.client.post(f"{self.base}{endpoint}", json=body)
                    status = response.status_code
                    if status == 404:
                        raise StageError("ollama_model_missing")
                    if status >= 500:
                        raise StageError("ollama_server", retry=True)
                    if status != 200:
                        # Ollama rejects oversized multimodal input before generation starts.
                        if status == 400 and "exceeds the available context size" in response.text:
                            raise StageError("ollama_context")
                        raise StageError("ollama_input")
                    result = response.json()
                    if not isinstance(result, dict):
                        raise StageError("ollama_response", retry=True)
                    return result
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise StageError("ollama_timeout", retry=True) from exc
        except httpx.RequestError as exc:
            raise StageError("ollama_unavailable", retry=True) from exc
        except ValueError as exc:
            raise StageError("ollama_response", retry=True) from exc
        finally:
            log.info("ollama_request", extra={"metrics": {
                "endpoint": endpoint, "model": body["model"], "interactive": interactive,
                "status": status, "ms": round((time.monotonic() - started) * 1000),
            }})

    async def embed(self, value: str, *, interactive: bool, model_digest: str) -> list[float]:
        if not value.strip():
            raise StageError("embedding_empty_text")
        data = await self.request("/api/embed", {
            "model": settings.embedding_model, "input": value, "truncate": False,
        }, interactive=interactive)
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != 1 or not isinstance(vectors[0], list):
            raise StageError("embedding_response", retry=True)
        vector = vectors[0]
        if len(vector) != settings.embedding_dimensions:
            raise StageError("embedding_dimensions")
        if not all(type(value) in (float, int) and math.isfinite(value) for value in vector) or not any(vector):
            raise StageError("embedding_invalid", retry=True)
        if await self.digest(settings.embedding_model) != model_digest:
            raise StageError("ollama_model_changed", retry=True)
        return vector

    async def structured(self, model: str, messages: list[dict], schema: type[Schema],
                         *, interactive: bool, num_predict: int, num_ctx: int = 8192) -> Schema:
        output_schema = schema.model_json_schema()
        # Identify the instruction/schema contract without logging user or source text.
        log.info("ai_contract", extra={"metrics": {
            "model": model,
            "prompt_version": fingerprint([message.get("content", "") for message in messages
                                           if message.get("role") == "system"]),
            "schema_version": fingerprint(output_schema),
        }})
        while True:
            try:
                result = await self.request("/api/chat", {
                    "model": model, "messages": messages, "format": output_schema,
                    "options": {"temperature": 0, "num_ctx": num_ctx, "num_predict": num_predict},
                    "think": False, "stream": False,
                }, interactive=interactive)
                # Multimodal input can fit while leaving no room for a complete JSON answer.
                prompt_tokens = result.get("prompt_eval_count", 0)
                if (result.get("done_reason") == "length" and type(prompt_tokens) is int
                        and prompt_tokens + num_predict > num_ctx and num_ctx < 32768):
                    num_ctx = min(num_ctx * 2, 32768)
                    continue
                if result.get("done_reason") == "length":
                    raise StageError("ollama_output_truncated")
                break
            except StageError as exc:
                if exc.code != "ollama_context" or num_ctx >= 32768:
                    raise
                num_ctx = min(num_ctx * 2, 32768)
        try:
            return schema.model_validate_json(result["message"]["content"])
        except (KeyError, TypeError, ValidationError) as exc:
            raise StageError("ollama_invalid_json", retry=True) from exc
