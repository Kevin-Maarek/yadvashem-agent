from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import settings
from schemas.research import ResearchCitation, ResearchResult

SourceKind = Literal["live"]
Action = Literal["pause", "resume", "restart"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobAction(StrictModel):
    job_id: int = Field(gt=0)
    acknowledge_access: bool = False
    confirm_erase: bool = False


class ChatRequest(StrictModel):
    message: str = Field(min_length=1, max_length=1200)
    source: SourceKind = "live"
    limit: int = Field(settings.search_limit, ge=1, le=15)
    threshold: float = Field(settings.search_threshold, ge=0, le=1)
    top_only: bool = False
    conversation_id: UUID | None = None
    request_id: UUID | None = None

    @model_validator(mode="after")
    def check_message(self):
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("A search message is required")
        return self


class ImageResult(BaseModel):
    image_id: int
    photo_id: int
    title: str
    catalog_description: str
    vision_description: str | None
    subjects: list[str]
    metadata: dict
    provenance: dict
    image_url: str | None
    back_url: str | None
    source_url: str | None
    source: SourceKind
    relative_score: float
    ranks: dict[str, int]
    fused_score: float


class Citation(BaseModel):
    photo_id: int
    title: str
    url: str


class ChatResult(BaseModel):
    text: str
    images: list[ImageResult]
    citations: list[Citation]
    requirements: list[str] = Field(default_factory=list)
    search_query: str | None = None
    semantic_available: bool
    notices: list[str] = Field(default_factory=list)
    conversation_id: UUID | None = None
    action: str = "search"
    research: ResearchResult | None = None
    research_citations: list[ResearchCitation] = Field(default_factory=list)
