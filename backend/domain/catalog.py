"""Catalog-only evidence returned alongside, but separate from, image observations."""
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from core.text import lexical


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogEntity(StrictModel):
    value: Annotated[str, StringConstraints(min_length=1, max_length=250)]
    source_field: Annotated[str, StringConstraints(min_length=1, max_length=300)]


class CatalogYears(StrictModel):
    start: int = Field(ge=1000, le=2100)
    end: int = Field(ge=1000, le=2100)
    source_field: Annotated[str, StringConstraints(min_length=1, max_length=300)]
    evidence: Annotated[str, StringConstraints(min_length=1, max_length=600)] = Field(
        description="Verbatim catalog evidence for the endpoint years.")

    @model_validator(mode="after")
    def ordered(self):
        if self.start > self.end:
            raise ValueError("Start year must precede end year")
        return self


class CatalogExtraction(StrictModel):
    names: list[CatalogEntity] = Field(max_length=40)
    places: list[CatalogEntity] = Field(max_length=40)
    years: CatalogYears | None = Field(
        description="Catalog photograph/event year range, when available.")


class PhotoEnrichment(StrictModel):
    description: Annotated[str, StringConstraints(min_length=1, max_length=16000)]
    catalog_extraction: CatalogExtraction


def catalog_fields(record) -> dict[str, str]:
    """Stable source paths, without feeding previously generated data back to the model."""
    result = {}

    def visit(path, value):
        if isinstance(value, str) and value.strip():
            result[path] = value
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(f"{path}[{index}]", item)
        elif isinstance(value, dict):
            for key, item in sorted(value.items()):
                visit(f"{path}.{key}", item)

    visit("title", record.title)
    visit("catalog_description", record.catalog_description)
    visit("subjects", record.subjects)
    visit("metadata_fields", {key: value for key, value in record.metadata_fields.items()
                              if key != "catalog_extracted"})
    return result


TEXT_FIELDS = {"title", "catalog_description"}
NAME_FIELD = re.compile(r"metadata_fields\.cluster(?:\[\d+\])?")
PLACE_FIELD = re.compile(r"metadata_fields\.(?:place(?:\[\d+\])?|map\[\d+\]\.places\[\d+\]\.places\[\d+\])")


def grounded_extraction(extraction: CatalogExtraction, fields: dict[str, str]) -> CatalogExtraction:
    """Only accept verbatim evidence from fields with the appropriate catalog role."""
    def entities(items, allowed):
        accepted, seen = [], set()
        for item in items:
            value = lexical(item.value)
            field = fields.get(item.source_field, "")
            if (not value or "\x00" in item.value
                    or not allowed(item.source_field) or value not in lexical(field)):
                continue
            key = (value, item.source_field)
            if key not in seen:
                accepted.append(item)
                seen.add(key)
        return accepted

    years = extraction.years
    if years:
        field = fields.get(years.source_field, "")
        evidence = lexical(years.evidence)
        if (years.source_field not in TEXT_FIELDS or not evidence or evidence not in lexical(field)
                or not all(re.search(rf"(?<!\d){year}(?!\d)", evidence)
                           for year in (years.start, years.end))):
            years = None
    return CatalogExtraction(
        names=entities(extraction.names, lambda field: field in TEXT_FIELDS or bool(NAME_FIELD.fullmatch(field))),
        places=entities(extraction.places, lambda field: field in TEXT_FIELDS or bool(PLACE_FIELD.fullmatch(field))),
        years=years,
    )
