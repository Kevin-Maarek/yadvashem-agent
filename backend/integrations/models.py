"""Route generation and embeddings independently while preserving Ollama digests."""
import asyncio

import httpx

from core.config import settings
from integrations.ollama import Ollama
from integrations.openai_compatible import OpenAICompatible


class Models:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        providers = {provider: Ollama(client) if provider == "ollama" else OpenAICompatible(client, provider)
                     for provider in {settings.ai_provider, settings.embedding_provider}}
        self.generation = providers[settings.ai_provider]
        self.embeddings = providers[settings.embedding_provider]

    async def profiles(self) -> dict[str, str]:
        if self.generation is self.embeddings:
            return await self.generation.profiles()
        generation, embeddings = await asyncio.gather(self.generation.profiles(), self.embeddings.profiles())
        return {**{model: generation[model] for model in (settings.chat_model, settings.vision_model)
                   if model in generation},
                **({settings.embedding_model: embeddings[settings.embedding_model]}
                   if settings.embedding_model in embeddings else {})}

    async def digest(self, model: str) -> str:
        provider = self.embeddings if model == settings.embedding_model else self.generation
        return await provider.digest(model)

    async def structured(self, *args, **kwargs):
        return await self.generation.structured(*args, **kwargs)

    async def vision(self, *args, **kwargs):
        return await self.generation.vision(*args, **kwargs)

    async def embed(self, *args, **kwargs):
        return await self.embeddings.embed(*args, **kwargs)


def create_models(client: httpx.AsyncClient) -> Ollama | Models:
    if settings.ai_provider == settings.embedding_provider == "ollama":
        return Ollama(client)
    return Models(client)
