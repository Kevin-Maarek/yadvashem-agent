from datetime import UTC, datetime

from sqlalchemy import select

from integrations.ollama import prepare_image, prompt_for
from integrations.models import create_models
from core.config import settings
from domain.catalog import catalog_fields
from db.session import Session
from services.ingestion.jobs import add_task
from db.models import CatalogEmbedding, Image, Record, Task, VisionDescription, VisionEmbedding
from integrations.yadvashem import Source, normalize, photo_id
from core.errors import StageError
from core.hashing import fingerprint
from core.text import lexical

def apply_catalog_extractions(session, record, catalog_key, version, digest):
    """Rebuild derived catalog columns from current active-image checkpoints."""
    if fingerprint(catalog_fields(record)) != catalog_key:
        raise StageError("catalog_changed", retry=True)
    session.flush()
    descriptions = session.scalars(
        select(VisionDescription).join(Image, Image.id == VisionDescription.image_id).where(
            Image.record_id == record.id, Image.active,
            VisionDescription.reference_fingerprint == Image.reference_fingerprint,
            VisionDescription.catalog_input_fingerprint == catalog_key,
            VisionDescription.prompt_version == version, VisionDescription.model_digest == digest,
        ).order_by(Image.position, Image.id)).all()
    names, places, candidates = {}, {}, []
    contributors = {"names_norm": [], "places_norm": [], "years": []}
    for description in descriptions:
        extraction = description.catalog_extraction
        profile = {"image_id": description.image_id, "model": description.model,
                   "model_digest": description.model_digest, "prompt_version": description.prompt_version}
        for key, target, column in (("names", names, "names_norm"), ("places", places, "places_norm")):
            for item in extraction.get(key, []):
                target.setdefault(lexical(item["value"]), item["value"])
                contributors[column].append({**item, **profile})
        if years := extraction.get("years"):
            candidates.append((years["start"], years["end"]))
            contributors["years"].append({**years, **profile})
    bounds = set(candidates)
    years = next(iter(bounds)) if len(bounds) == 1 else (None, None)
    record.names_norm = list(names)
    record.places_norm = list(places)
    record.year_start, record.year_end = years
    record.metadata_fields = {**record.metadata_fields, "catalog_extracted": {
        "names": list(names.values()), "places": list(places.values()),
        "years": {"start": years[0], "end": years[1]} if years[0] is not None else None,
    }}
    provenance = dict(record.provenance)
    for column in ("names_norm", "places_norm"):
        provenance[column] = {"source": "llm_catalog", "contributors": contributors[column],
                              "catalog_input_fingerprint": catalog_key}
    for column in ("year_start", "year_end"):
        provenance[column] = {"source": "llm_catalog", "contributors": contributors["years"],
                              "conflicting": len(bounds) > 1, "catalog_input_fingerprint": catalog_key}
    record.provenance = provenance
    # Start from original catalog data so retries and re-ingestion do not accumulate old terms.
    normalized, _ = normalize(record.source, record.photo_id, record.raw_search, record.raw_details)
    year_terms = [str(year) for year in dict.fromkeys(years) if year is not None]
    record.lexical = lexical(" ".join([normalized["lexical"], *names.values(), *places.values(), *year_terms]))


async def count_records(source):
    reported = await source.count()

    def apply(session, job, task):
        job.scope = {**job.scope, "reported_total": reported,
                     "reported_total_at": datetime.now(UTC).isoformat()}

    return apply


async def discover(source, task):
    page = task.payload["page"]
    cards, count = await source.page(page)
    page_fingerprint = fingerprint(sorted({photo_id(card["id"]) for card in cards}))
    if cards and page_fingerprint == task.payload.get("previous_page"):
        raise StageError("source_repeated_page")

    def apply(session, job, task):
        if count is not None:
            job.scope = {**job.scope, "search_reported_total": count,
                         "search_reported_total_at": datetime.now(UTC).isoformat()}
        for card in cards:
            number = photo_id(card["id"])
            add_task(session, job.id, "details", f"photo:{number}", photo_id=number, card=card)
        if cards:
            add_task(session, job.id, "discovery", f"page:{page + 1}", page=page + 1,
                     previous_page=page_fingerprint)

    return apply


async def details(source, task):
    raw_details = await source.details(task.photo_id)
    raw_search = task.payload["card"]
    normalized, images = normalize(source.kind, task.photo_id, raw_search, raw_details)

    def apply(session, job, task):
        record = session.scalar(select(Record).where(Record.source == job.source, Record.photo_id == task.photo_id))
        if record is None:
            record = Record(source=job.source, photo_id=task.photo_id, **normalized)
            session.add(record)
            session.flush()
        else:
            for key, value in normalized.items():
                setattr(record, key, value)
        existing = {image.identity: image for image in session.scalars(
            select(Image).where(Image.record_id == record.id)).all()}
        for image in existing.values():
            image.active = False
        add_task(session, job.id, "catalog", f"catalog:{record.id}", photo_id=task.photo_id, record_id=record.id)
        for values in images:
            image = existing.get(values["identity"])
            if image is None:
                image = Image(record_id=record.id, **values)
                session.add(image)
                session.flush()
            else:
                if image.reference_fingerprint != values["reference_fingerprint"]:
                    image.status = "pending"
                for key, value in values.items():
                    setattr(image, key, value)
                image.active = True
                prior = session.get(VisionDescription, image.id)
                if not prior or prior.catalog_input_fingerprint != fingerprint(catalog_fields(record)):
                    image.status = "pending"
            add_task(session, job.id, "vision", f"vision:{image.id}", photo_id=task.photo_id, image_id=image.id)
        if not images:
            add_task(session, job.id, "missing_image", f"missing:{record.id}", photo_id=task.photo_id)

    return apply


async def catalog(ollama, task):
    digest = await ollama.digest(settings.embedding_model)
    with Session() as session:
        record = session.get(Record, task.payload["record_id"])
        if record is None:
            raise StageError("record_missing")
        input_key = fingerprint(record.catalog_fingerprint, digest, settings.embedding_dimensions)
        existing = session.get(CatalogEmbedding, record.id)
        reuse = existing and existing.input_fingerprint == input_key
    vector = None if reuse else await ollama.embed(record.catalog_text, interactive=False, model_digest=digest)

    def apply(session, job, task):
        if vector is not None:
            session.merge(CatalogEmbedding(record_id=record.id, vector=vector, input_fingerprint=input_key,
                                           catalog_fingerprint=record.catalog_fingerprint,
                                           model=settings.embedding_model, model_digest=digest))
        for image in session.scalars(select(Image).where(Image.record_id == record.id, Image.active)):
            add_task(session, job.id, "save", f"save:{image.id}", photo_id=task.photo_id, image_id=image.id)
        return "skipped" if reuse else None

    return apply


async def vision(source, ollama, task):
    prompt, version = prompt_for()
    digest = await ollama.digest(settings.vision_model)
    with Session() as session:
        image = session.get(Image, task.image_id)
        if image is None or not image.active:
            return lambda *_: "skipped"
        prior = session.get(VisionDescription, image.id)
        record = session.get(Record, image.record_id)
        fields = catalog_fields(record)
        catalog_key = fingerprint(fields)
    matches_refs = prior and prior.reference_fingerprint == image.reference_fingerprint
    matches_profile = prior and prior.model_digest == digest and prior.prompt_version == version
    matches_catalog = prior and prior.catalog_input_fingerprint == catalog_key
    existing = matches_refs and matches_profile and matches_catalog
    values = None
    if not existing:
        prepared = []
        hashes = []
        for reference in (image.front_reference, image.back_reference):
            if reference:
                raw, media_type = await source.image(reference)
                encoded, image_hash = prepare_image(raw, media_type)
                del raw
                prepared.append(encoded)
                hashes.append(image_hash)
        if not prepared:
            raise StageError("image_missing")
        content = fingerprint(*hashes)
        input_key = fingerprint(image.reference_fingerprint, content, digest, version)
        input_key = fingerprint(input_key, catalog_key)
        if not prior or prior.input_fingerprint != input_key:
            result = await ollama.vision(prompt, prepared, digest, catalog=fields)
            description = result.description
            extraction = result.catalog_extraction.model_dump()
            values = {
                "image_id": image.id, "description": description, "lexical": lexical(description),
                "model": settings.vision_model, "model_digest": digest, "prompt_version": version,
                "reference_fingerprint": image.reference_fingerprint, "content_fingerprint": content,
                "input_fingerprint": input_key,
                "catalog_input_fingerprint": catalog_key, "catalog_extraction": extraction,
            }
        del prepared

    def apply(session, job, task):
        if values:
            session.merge(VisionDescription(**values))
            current = session.get(Image, image.id)
            current.status = "pending"
        current_record = session.get(Record, image.record_id)
        apply_catalog_extractions(session, current_record, catalog_key, version, digest)
        add_task(session, job.id, "vision_embedding", f"vision-vector:{image.id}", photo_id=task.photo_id,
                 image_id=image.id, vision_generated=bool(values))
        return None if values else "skipped"

    return apply


async def vision_embedding(ollama, task):
    digest = await ollama.digest(settings.embedding_model)
    with Session() as session:
        image = session.get(Image, task.image_id)
        if image is None or not image.active:
            return lambda *_: "skipped"
        description = session.get(VisionDescription, image.id)
        if description is None or description.reference_fingerprint != image.reference_fingerprint:
            raise StageError("vision_checkpoint")
        input_key = fingerprint(description.description, digest, settings.embedding_dimensions)
        existing = session.get(VisionEmbedding, image.id)
        reuse = existing and existing.input_fingerprint == input_key
    vector = None if reuse else await ollama.embed(description.description, interactive=False, model_digest=digest)

    def apply(session, job, task):
        if vector is not None:
            session.merge(VisionEmbedding(image_id=image.id, vector=vector, input_fingerprint=input_key,
                                          model=settings.embedding_model, model_digest=digest))
        return "skipped" if reuse and not task.payload.get("vision_generated") else None

    return apply


def save_image(task):
    def apply(session, job, task):
        image = session.get(Image, task.image_id)
        if image is None or not image.active:
            return "skipped"
        description = session.get(VisionDescription, image.id)
        vector = session.get(VisionEmbedding, image.id)
        catalog = session.get(CatalogEmbedding, image.record_id)
        record = session.get(Record, image.record_id)
        completed = set(session.scalars(select(Task.stage).where(
            Task.job_id == job.id, Task.photo_id == task.photo_id,
            Task.status.in_(("succeeded", "skipped")),
            (Task.image_id == image.id) | (Task.stage == "catalog"),
        )))
        if not {"vision", "vision_embedding", "catalog"} <= completed:
            raise StageError("vision_checkpoint")
        if not description or not vector or not catalog or catalog.catalog_fingerprint != record.catalog_fingerprint:
            raise StageError("vision_checkpoint")
        if (description.reference_fingerprint != image.reference_fingerprint
                or description.catalog_input_fingerprint != fingerprint(catalog_fields(record))
                or vector.model_digest != catalog.model_digest):
            raise StageError("vision_checkpoint")
        image.status = "indexed"
    return apply


async def run_stage(client, job, task):
    source = Source(client, job.source, client_id=job.scope.get("source_client_id"))
    ollama = create_models(client)
    if task.stage == "count":
        return await count_records(source)
    if task.stage == "discovery":
        return await discover(source, task)
    if task.stage == "details":
        return await details(source, task)
    if task.stage == "catalog":
        return await catalog(ollama, task)
    if task.stage == "vision":
        return await vision(source, ollama, task)
    if task.stage == "vision_embedding":
        return await vision_embedding(ollama, task)
    if task.stage == "save":
        return save_image(task)
    if task.stage == "missing_image":
        raise StageError("image_missing")
    raise StageError("stage_unknown")
