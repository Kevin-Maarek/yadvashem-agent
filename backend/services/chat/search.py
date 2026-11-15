import json
import logging
import re
import time

from sqlalchemy import bindparam, select, text

from schemas.api import ChatRequest, ImageResult
from db.session import Session
from db.models import Image, Record
from core.text import lexical, signature_key

log = logging.getLogger(__name__)
RRF_K = 60
WEIGHTS = {"catalog": 1.0, "vision": 1.0, "lexical": 1.0, "exact": 2.0}

ELIGIBLE = """
    SELECT i.id, i.record_id, i.position
    FROM logical_images i
    JOIN archival_records r ON r.id = i.record_id
    JOIN catalog_embeddings c ON c.record_id = r.id AND c.catalog_fingerprint = r.catalog_fingerprint
    JOIN vision_descriptions d ON d.image_id = i.id AND d.reference_fingerprint = i.reference_fingerprint
    JOIN vision_embeddings v ON v.image_id = i.id AND v.model_digest = c.model_digest
    WHERE i.active AND i.status = 'indexed' AND r.source = :source
"""


def rank_fusion(channels: dict[str, dict[int, int]]) -> list[tuple[int, float, dict[str, int]]]:
    ranks: dict[int, dict[str, int]] = {}
    for channel, positions in channels.items():
        for image_id, rank in positions.items():
            ranks.setdefault(image_id, {})[channel] = rank
    scores = [(image_id, sum(WEIGHTS[channel] / (RRF_K + rank) for channel, rank in evidence.items()), evidence)
              for image_id, evidence in ranks.items()]
    return sorted(scores, key=lambda row: (-row[1], row[0]))


def lexical_terms(query: str) -> str:
    terms = dict.fromkeys(re.findall(r"[^\W_]+", lexical(query), flags=re.UNICODE))
    return " OR ".join(list(terms)[:32])


def ranked_channel(rows) -> dict[int, int]:
    return {image_id: rank for rank, image_id in enumerate(rows, 1)}


def load_results(session, ordered: list[tuple[int, float, dict[str, int]]]) -> list[ImageResult]:
    if not ordered:
        return []
    ids = [item[0] for item in ordered]
    rows = session.execute(text("""
        SELECT i.id AS image_id, r.photo_id, r.title, r.catalog_description, d.description AS vision_description,
               r.subjects, r.metadata_fields AS metadata, r.provenance, i.front_url AS image_url, i.back_url,
               r.source_url, r.source
        FROM logical_images i JOIN archival_records r ON r.id = i.record_id
        LEFT JOIN vision_descriptions d ON d.image_id = i.id AND d.reference_fingerprint = i.reference_fingerprint
        WHERE i.id IN :ids
    """).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings().all()
    by_id = {row["image_id"]: row for row in rows}
    return [
        ImageResult(**by_id[image_id], fused_score=score, ranks=ranks,
                    relative_score=round(score / ordered[0][1], 6) if ordered[0][1] else 1)
        for image_id, score, ranks in ordered if image_id in by_id
    ]


def result_limit(request: ChatRequest) -> int:
    return 1 if request.top_only else request.limit


def select_results(request: ChatRequest, images: list[ImageResult]) -> list[ImageResult]:
    """Apply display settings relative to the best accepted result, not rejected candidates."""
    if not images:
        return []
    best = images[0].fused_score
    return [image.model_copy(update={
        "relative_score": round(image.fused_score / best, 6) if best else 1,
    }) for image in images if image.fused_score >= best * request.threshold][:result_limit(request)]


def hybrid(request: ChatRequest, query: str, vector: list[float] | None,
           digest: str | None, *, channels: set[str] | None = None,
           candidate_limit: int | None = None) -> list[ImageResult]:
    started = time.monotonic()
    enabled = set(WEIGHTS) if channels is None else channels
    params = {"source": request.source, "query": lexical(query), "tokens": lexical_terms(query),
              "exact_query": lexical(request.message), "signature": signature_key(request.message),
              "digest": digest, "pool": max(40, request.limit * 6)}
    if vector is not None:
        params["embedding"] = json.dumps(vector, separators=(",", ":"))
    eligible = ELIGIBLE
    candidates = {}
    with Session() as session:
        # One snapshot keeps RRF inputs and displayed metadata consistent during ingestion.
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        if vector is not None and "catalog" in enabled:
            rows = session.execute(text(f"""
                WITH eligible AS ({eligible}),
                ranked AS (
                    SELECT c.record_id, row_number() OVER (
                        ORDER BY c.vector <=> CAST(:embedding AS vector), c.record_id
                    ) AS rank
                    FROM catalog_embeddings c
                    WHERE c.model_digest = :digest AND EXISTS (
                        SELECT 1 FROM eligible e WHERE e.record_id = c.record_id
                    )
                    ORDER BY rank LIMIT :pool
                )
                SELECT e.id, ranked.rank FROM ranked JOIN eligible e ON e.record_id = ranked.record_id
                ORDER BY ranked.rank, e.position, e.id
            """), params)
            candidates["catalog"] = {image_id: rank for image_id, rank in rows}
        if vector is not None and "vision" in enabled:
            rows = session.scalars(text(f"""
                WITH eligible AS ({eligible})
                SELECT e.id FROM eligible e JOIN vision_embeddings v ON v.image_id = e.id
                WHERE v.model_digest = :digest
                ORDER BY v.vector <=> CAST(:embedding AS vector), e.id LIMIT :pool
            """), params)
            candidates["vision"] = ranked_channel(rows)
        if params["tokens"] and "lexical" in enabled:
            session.execute(text("SET LOCAL pg_trgm.word_similarity_threshold = 0.3"))
            rows = session.scalars(text(f"""
                WITH eligible AS ({eligible}),
                q AS (SELECT websearch_to_tsquery('simple', :tokens) AS terms)
                SELECT e.id
                FROM eligible e JOIN archival_records r ON r.id = e.record_id
                JOIN vision_descriptions d ON d.image_id = e.id CROSS JOIN q
                WHERE to_tsvector('simple', r.lexical) @@ q.terms
                   OR to_tsvector('simple', d.lexical) @@ q.terms
                   OR r.lexical %> :query OR d.lexical %> :query
                ORDER BY GREATEST(
                    1.2 * ts_rank_cd(to_tsvector('simple', r.lexical), q.terms, 32),
                    ts_rank_cd(to_tsvector('simple', d.lexical), q.terms, 32),
                    0.3 * word_similarity(:query, r.lexical),
                    0.3 * word_similarity(:query, d.lexical)
                ) DESC, e.id LIMIT :pool
            """), params)
            candidates["lexical"] = ranked_channel(rows)
        if params["query"] and "exact" in enabled:
            rows = session.execute(text(f"""
                WITH eligible AS ({eligible})
                SELECT e.id, dense_rank() OVER (ORDER BY CASE
                    WHEN r.signature_norm = :signature THEN 1
                    WHEN r.title_norm IN (:query, :exact_query) THEN 2 ELSE 3 END) AS rank
                FROM eligible e JOIN archival_records r ON r.id = e.record_id
                WHERE (r.signature_norm <> '' AND r.signature_norm = :signature)
                   OR r.title_norm IN (:query, :exact_query) OR r.names_norm @> ARRAY[CAST(:exact_query AS text)]
                ORDER BY rank, e.position, e.id LIMIT :pool
            """), params)
            candidates["exact"] = {image_id: rank for image_id, rank in rows}
        ranked = rank_fusion(candidates)
        if ranked and candidate_limit is None:
            ranked = [item for item in ranked if item[1] >= ranked[0][1] * request.threshold]
        results = load_results(session, ranked[:candidate_limit if candidate_limit is not None else result_limit(request)])
    log.info("hybrid_search", extra={"metrics": {"results": len(results), "channels": list(candidates),
                                                  "ms": round((time.monotonic() - started) * 1000)}})
    return results


def context_images(source: str, image_ids: list[int]) -> list[ImageResult]:
    """Reload only known active images from the requested source."""
    if not image_ids:
        return []
    with Session() as session:
        query = select(Image.id).join(Record).where(
            Image.id.in_(image_ids), Image.active, Record.source == source)
        allowed = set(session.scalars(query).all())
        return load_results(session, [(image_id, 0, {}) for image_id in image_ids if image_id in allowed])


def details_lookup(request: ChatRequest, *, number: int | None = None, signature: str | None = None,
                   candidate_limit: int | None = None):
    with Session() as session:
        query = select(Record).where(Record.source == request.source)
        if number is not None:
            query = query.where(Record.photo_id == number)
        else:
            query = query.where(Record.signature_norm == signature_key(signature or ""), Record.signature_norm != "")
        records = session.scalars(query.order_by(Record.id).limit(result_limit(request))).all()
        if not records:
            return [], None
        ids = session.scalars(select(Image.id).where(Image.record_id.in_([record.id for record in records]), Image.active)
                              .order_by(Image.record_id, Image.position, Image.id)
                              .limit(candidate_limit or result_limit(request)))
        return load_results(session, [(image_id, 0, {}) for image_id in ids]), records[0]
