from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.config import settings


class Base(DeclarativeBase):
    type_annotation_map = {datetime: DateTime(timezone=True), dict[str, Any]: JSONB}


class Record(Base):
    __tablename__ = "archival_records"
    __table_args__ = (
        CheckConstraint("source = 'live'", name="ck_archival_records_source"),
        UniqueConstraint("source", "photo_id"),
        Index("ix_record_names", "names_norm", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str]
    photo_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_url: Mapped[str | None]
    title: Mapped[str]
    title_norm: Mapped[str] = mapped_column(index=True)
    catalog_description: Mapped[str]
    subjects: Mapped[list[str]] = mapped_column(ARRAY(Text))
    signature_norm: Mapped[str] = mapped_column(index=True)
    names_norm: Mapped[list[str]] = mapped_column(ARRAY(Text))
    places_norm: Mapped[list[str]] = mapped_column(ARRAY(Text))
    year_start: Mapped[int | None]
    year_end: Mapped[int | None]
    collection_key: Mapped[str | None] = mapped_column(index=True)
    metadata_fields: Mapped[dict[str, Any]]
    provenance: Mapped[dict[str, Any]]
    raw_search: Mapped[dict[str, Any] | None]
    raw_details: Mapped[dict[str, Any]]
    catalog_text: Mapped[str]
    catalog_fingerprint: Mapped[str]
    lexical: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Image(Base):
    __tablename__ = "logical_images"
    __table_args__ = (UniqueConstraint("record_id", "identity"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("archival_records.id"), index=True)
    identity: Mapped[str]
    position: Mapped[int]
    front_reference: Mapped[str]
    back_reference: Mapped[str | None]
    front_url: Mapped[str | None]
    back_url: Mapped[str | None]
    reference_fingerprint: Mapped[str]
    active: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(default="pending")


class CatalogEmbedding(Base):
    __tablename__ = "catalog_embeddings"

    record_id: Mapped[int] = mapped_column(ForeignKey("archival_records.id"), primary_key=True)
    vector: Mapped[list[float]] = mapped_column(VECTOR(settings.embedding_dimensions))
    catalog_fingerprint: Mapped[str]
    input_fingerprint: Mapped[str]
    model: Mapped[str]
    model_digest: Mapped[str]


class VisionDescription(Base):
    __tablename__ = "vision_descriptions"

    image_id: Mapped[int] = mapped_column(ForeignKey("logical_images.id"), primary_key=True)
    description: Mapped[str]
    lexical: Mapped[str]
    model: Mapped[str]
    model_digest: Mapped[str]
    prompt_version: Mapped[str]
    reference_fingerprint: Mapped[str]
    content_fingerprint: Mapped[str]
    input_fingerprint: Mapped[str]
    catalog_input_fingerprint: Mapped[str] = mapped_column(default="", server_default="")
    catalog_extraction: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


class VisionEmbedding(Base):
    __tablename__ = "vision_embeddings"

    image_id: Mapped[int] = mapped_column(ForeignKey("logical_images.id"), primary_key=True)
    vector: Mapped[list[float]] = mapped_column(VECTOR(settings.embedding_dimensions))
    input_fingerprint: Mapped[str]
    model: Mapped[str]
    model_digest: Mapped[str]


class Job(Base):
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("source = 'live'", name="ck_ingestion_jobs_source"),
        CheckConstraint("status IN ('queued', 'running', 'pausing', 'paused', 'restarting', 'completed', 'completed_with_errors')",
                        name="ck_ingestion_status"),
        Index("uq_ingestion_singleton", text("(true)"), unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str]
    status: Mapped[str] = mapped_column(index=True)
    scope: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class Task(Base):
    __tablename__ = "ingestion_tasks"
    __table_args__ = (
        UniqueConstraint("job_id", "task_key"),
        Index("ix_task_claim", "job_id", "status", "available_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("ingestion_jobs.id"), index=True)
    task_key: Mapped[str]
    stage: Mapped[str]
    payload: Mapped[dict[str, Any]]
    photo_id: Mapped[int | None] = mapped_column(BigInteger)
    image_id: Mapped[int | None] = mapped_column(ForeignKey("logical_images.id"))
    status: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    claim_token: Mapped[str | None]
    error_code: Mapped[str | None]


class Attempt(Base):
    __tablename__ = "ingestion_attempts"
    __table_args__ = (UniqueConstraint("task_id", "number"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("ingestion_tasks.id"), index=True)
    number: Mapped[int]
    status: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    error_code: Mapped[str | None]


class SourceGate(Base):
    __tablename__ = "source_gate"

    id: Mapped[int] = mapped_column(primary_key=True)
    next_request_at: Mapped[datetime | None]
    blocked: Mapped[bool] = mapped_column(default=False)


class ChatWaiter(Base):
    __tablename__ = "chat_waiters"

    id: Mapped[str] = mapped_column(primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(index=True)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"

    id: Mapped[int] = mapped_column(primary_key=True)
    last_seen: Mapped[datetime]


class Conversation(Base):
    __tablename__ = "chat_conversations"

    id: Mapped[str] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(default=0)
    state: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ConversationTurn(Base):
    __tablename__ = "chat_turns"
    __table_args__ = (UniqueConstraint("conversation_id", "request_id"),
                      UniqueConstraint("conversation_id", "sequence"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True)
    request_id: Mapped[str]
    sequence: Mapped[int]
    message: Mapped[str]
    result: Mapped[dict[str, Any]]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
