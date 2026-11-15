"""Shared image enrichment and embedding validation across model providers."""
import json
import math
import re

from core.config import settings
from core.errors import StageError
from domain.catalog import PhotoEnrichment, grounded_extraction
from prompts import load_prompt


def validate_embedding(vector) -> list[float]:
    if not isinstance(vector, list):
        raise StageError("embedding_response", retry=True)
    if len(vector) != settings.embedding_dimensions:
        raise StageError("embedding_dimensions")
    if not all(type(value) in (float, int) and math.isfinite(value) for value in vector) or not any(vector):
        raise StageError("embedding_invalid", retry=True)
    return vector


class VisionModel:
    async def vision(self, prompt: str, images: list[str], model_digest: str,
                     *, catalog: dict[str, str]) -> PhotoEnrichment:
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({"CATALOG_SOURCE_FIELDS": catalog},
                                                    ensure_ascii=False, sort_keys=True), "images": images},
        ]
        try:
            result = await self.structured(settings.vision_model, messages, PhotoEnrichment,
                                           interactive=False, num_predict=settings.vision_output_tokens, num_ctx=16384)
        except StageError as exc:
            if exc.code not in ("ollama_output_truncated", "ollama_invalid_json",
                                "ai_output_truncated", "ai_invalid_json"):
                raise
            compact = load_prompt("vision.recovery.txt")
            result = await self.structured(settings.vision_model,
                                           [{"role": "system", "content": prompt + "\n\n" + compact}, messages[1]],
                                           PhotoEnrichment, interactive=False,
                                           num_predict=min(settings.vision_output_tokens, 2600), num_ctx=16384)
        if not re.search(r"[א-ת]", result.description) or "\x00" in result.description:
            raise StageError("vision_language", retry=True)
        if await self.digest(settings.vision_model) != model_digest:
            raise StageError("ai_model_changed" if settings.ai_provider != "ollama" else "ollama_model_changed",
                             retry=True)
        result.catalog_extraction = grounded_extraction(result.catalog_extraction, catalog)
        return result
