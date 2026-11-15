"""OpenAI and OpenRouter HTTP APIs; no provider SDK or local GPU lock required."""
import logging
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from core.config import settings
from core.errors import StageError
from core.hashing import fingerprint
from integrations.model_common import VisionModel, validate_embedding

log = logging.getLogger(__name__)
Schema = TypeVar("Schema", bound=BaseModel)


def strict_schema(value):
    """Require all fields for API strict mode; retain local Pydantic validation."""
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: strict_schema(item) for key, item in value.items() if key != "default"}
    if result.get("type") == "object":
        result["additionalProperties"] = False
        result["required"] = list(result.get("properties", {}))
    return result


class OpenAICompatible(VisionModel):
    def __init__(self, client: httpx.AsyncClient, provider: str):
        self.client = client
        self.provider = provider
        self.base = getattr(settings, f"{provider}_base_url").rstrip("/")
        self.key = getattr(settings, f"{provider}_api_key").get_secret_value()

    def identity(self, model: str) -> str:
        # API providers do not expose Ollama's immutable artifact digest.
        dimensions = settings.embedding_dimensions if model == settings.embedding_model else None
        return fingerprint(self.provider, self.base, model, settings.api_model_revision, dimensions)

    async def digest(self, model: str) -> str:
        return self.identity(model)

    async def request(self, method: str, endpoint: str, *, body=None) -> dict:
        try:
            response = await self.client.request(
                method, f"{self.base}{endpoint}", json=body,
                headers={"Authorization": f"Bearer {self.key}"}, timeout=settings.api_timeout_seconds,
            )
            status = response.status_code
            if status in (401, 403):
                raise StageError("ai_auth")
            if status == 429:
                raise StageError("ai_rate_limit", retry=True)
            if status >= 500:
                raise StageError("ai_server", retry=True)
            if status == 404:
                raise StageError("ai_model_missing")
            if not response.is_success:
                raise StageError("ai_input")
            result = response.json()
            if not isinstance(result, dict):
                raise StageError("ai_response", retry=True)
            # Some compatible APIs return errors inside a successful HTTP response.
            if result.get("error"):
                raise StageError("ai_response", retry=True)
            return result
        except httpx.TimeoutException as exc:
            raise StageError("ai_timeout", retry=True) from exc
        except httpx.RequestError as exc:
            raise StageError("ai_unavailable", retry=True) from exc
        except ValueError as exc:
            raise StageError("ai_response", retry=True) from exc

    async def profiles(self) -> dict[str, str]:
        endpoints = ["/models"]
        if self.provider == "openrouter" and settings.embedding_provider == "openrouter":
            endpoints = (["/models"] if settings.ai_provider == "openrouter" else []) + ["/embeddings/models"]
        try:
            profiles = {}
            for endpoint in endpoints:
                result = await self.request("GET", endpoint)
                profiles.update({item["id"]: self.identity(item["id"]) for item in result["data"]
                                 if isinstance(item.get("id"), str)})
            return profiles
        except (KeyError, TypeError, AttributeError) as exc:
            raise StageError("ai_response", retry=True) from exc

    async def structured(self, model: str, messages: list[dict], schema: type[Schema],
                         *, interactive: bool, num_predict: int, num_ctx: int = 8192) -> Schema:
        output_schema = strict_schema(schema.model_json_schema())
        log.info("ai_contract", extra={"metrics": {
            "provider": self.provider, "model": model,
            "prompt_version": fingerprint([message.get("content", "") for message in messages
                                           if message.get("role") == "system"]),
            "schema_version": fingerprint(output_schema),
        }})
        converted = []
        for message in messages:
            entry = {"role": message["role"], "content": message.get("content", "")}
            if message.get("images"):
                entry["content"] = [{"type": "text", "text": message.get("content", "")}] + [
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}
                    for image in message["images"]
                ]
            converted.append(entry)
        body = {
            "model": model, "messages": converted,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": schema.__name__, "strict": True, "schema": output_schema,
            }},
        }
        # OpenAI's reasoning models require max_completion_tokens and may reject temperature.
        body["max_completion_tokens" if self.provider == "openai" else "max_tokens"] = num_predict
        if self.provider == "openrouter":
            body["provider"] = {"require_parameters": True}
        result = await self.request("POST", "/chat/completions", body=body)
        try:
            choice = result["choices"][0]
            if choice.get("finish_reason") == "length":
                raise StageError("ai_output_truncated")
            if choice.get("finish_reason") == "content_filter" or choice["message"].get("refusal"):
                raise StageError("ai_refusal")
            return schema.model_validate_json(choice["message"]["content"])
        except (KeyError, IndexError, TypeError, AttributeError, ValidationError) as exc:
            raise StageError("ai_invalid_json", retry=True) from exc

    async def embed(self, value: str, *, interactive: bool, model_digest: str) -> list[float]:
        if not value.strip():
            raise StageError("embedding_empty_text")
        if model_digest != self.identity(settings.embedding_model):
            raise StageError("ai_model_changed", retry=True)
        result = await self.request("POST", "/embeddings", body={
            "model": settings.embedding_model, "input": value, "encoding_format": "float",
            "dimensions": settings.embedding_dimensions,
        })
        try:
            data = result["data"]
            if len(data) != 1 or data[0].get("index") != 0:
                raise StageError("embedding_response", retry=True)
            return validate_embedding(data[0]["embedding"])
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise StageError("embedding_response", retry=True) from exc
