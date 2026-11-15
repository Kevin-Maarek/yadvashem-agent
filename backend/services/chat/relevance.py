"""Batch relevance decisions grounded in the candidate evidence sent to the model."""
import json
from typing import Literal

from pydantic import Field

from core.errors import StageError
from schemas.api import ImageResult, StrictModel

CANDIDATE_LIMIT = 20


class Support(StrictModel):
    requirement_index: int = Field(ge=0, le=11)
    evidence_source: Literal["title", "catalog", "catalog_metadata", "ai_visual_observations"]
    evidence_quote: str = Field(min_length=1, max_length=240)


class Decision(StrictModel):
    image_id: int = Field(gt=0)
    verdict: Literal["supported", "uncertain", "irrelevant"]
    reason: str = Field(min_length=1, max_length=240)
    support: list[Support] = Field(default_factory=list, max_length=12)


class Review(StrictModel):
    decisions: list[Decision] = Field(max_length=CANDIDATE_LIMIT)


def candidate_evidence(images: list[ImageResult]) -> list[dict]:
    # Keep the batch bounded, with more detail per photo when fewer candidates exist.
    budget = 12000 // max(1, len(images))
    result = []
    for image in images:
        # Derived source-grounded entities come first, before potentially large raw map data.
        metadata = {"catalog_extracted": image.metadata.get("catalog_extracted"),
                    "subjects": image.subjects,
                    **{key: value for key, value in image.metadata.items() if key != "catalog_extracted"}}
        original = {"title": image.title, "catalog": image.catalog_description,
                    "catalog_metadata": json.dumps(metadata, ensure_ascii=False),
                    "ai_visual_observations": image.vision_description or ""}
        title_budget = min(len(image.title), 400, budget // 3)
        text_fields = [key for key in original if key != "title" and original[key]]
        text_budget = (budget - title_budget) // max(1, len(text_fields))
        fields = {"title": image.title[:title_budget],
                  **{key: value[:text_budget] for key, value in original.items() if key != "title"}}
        result.append({"image_id": image.image_id, **fields,
                       "truncated_fields": [key for key in fields if fields[key] != original[key]]})
    return result


def accepted_ids(review: Review, candidates: list[dict], requirements: list[str]) -> set[int]:
    by_id = {candidate["image_id"]: candidate for candidate in candidates}
    ids = [decision.image_id for decision in review.decisions]
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise StageError("relevance_invalid_decisions")
    accepted = set()
    for decision in review.decisions:
        if decision.verdict != "supported":
            continue
        # Every requirement needs its own source-grounded support, not just the main topic.
        covered = set()
        for support in decision.support:
            quote = " ".join(support.evidence_quote.split())
            source = " ".join(by_id[decision.image_id][support.evidence_source].split())
            if quote and quote in source:
                covered.add(support.requirement_index)
        if requirements and covered == set(range(len(requirements))):
            accepted.add(decision.image_id)
    return accepted
