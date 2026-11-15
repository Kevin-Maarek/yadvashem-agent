"""Public, backward-compatible person investigation results."""
from typing import Literal

from pydantic import BaseModel, Field


class ResearchCitation(BaseModel):
    source_id: str
    title: str
    url: str


class ResearchCandidate(BaseModel):
    cluster_id: str | None
    name: str
    birth_year: str | None = None
    places: list[str] = Field(default_factory=list)
    fate: str | None = None
    basis: Literal["source_linked", "possible", "user_selected"] = "possible"
    comparisons: list[dict] = Field(default_factory=list)


class ResearchResult(BaseModel):
    photo_id: int | None = None
    status: str
    candidates: list[ResearchCandidate] = Field(default_factory=list)
    total: int | None = None
    reviewed: int = 0
    has_more: bool = False
