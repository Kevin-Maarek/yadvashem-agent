"""Create the complete archive schema and initial paused collection process.

Revision ID: 0001_baseline
Revises:

Replaces revisions 0001 through 0007. Existing databases already at 0007 must
be backed up and stamped with 0001_baseline using --purge; do not run this
creation migration against an existing schema. See README.md.
"""
from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy
from sqlalchemy.dialects import postgresql

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('archival_records',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('photo_id', sa.BigInteger(), nullable=False),
        sa.Column('source_url', sa.String(), nullable=True),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('title_norm', sa.String(), nullable=False),
        sa.Column('catalog_description', sa.String(), nullable=False),
        sa.Column('subjects', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('signature_norm', sa.String(), nullable=False),
        sa.Column('names_norm', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('places_norm', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('year_start', sa.Integer(), nullable=True),
        sa.Column('year_end', sa.Integer(), nullable=True),
        sa.Column('collection_key', sa.String(), nullable=True),
        sa.Column('metadata_fields', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('raw_search', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('raw_details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('catalog_text', sa.String(), nullable=False),
        sa.Column('catalog_fingerprint', sa.String(), nullable=False),
        sa.Column('lexical', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("source = 'live'", name='ck_archival_records_source'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source', 'photo_id')
    )
    op.create_index(op.f('ix_archival_records_collection_key'), 'archival_records', ['collection_key'], unique=False)
    op.create_index(op.f('ix_archival_records_photo_id'), 'archival_records', ['photo_id'], unique=False)
    op.create_index(op.f('ix_archival_records_signature_norm'), 'archival_records', ['signature_norm'], unique=False)
    op.create_index(op.f('ix_archival_records_title_norm'), 'archival_records', ['title_norm'], unique=False)
    op.create_index('ix_record_names', 'archival_records', ['names_norm'], unique=False, postgresql_using='gin')
    op.create_table('chat_waiters',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_chat_waiters_expires_at'), 'chat_waiters', ['expires_at'], unique=False)
    op.create_table('ingestion_jobs',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('source', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('scope', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source = 'live'", name='ck_ingestion_jobs_source'),
        sa.CheckConstraint("status IN ('queued', 'running', 'pausing', 'paused', 'restarting', 'completed', 'completed_with_errors')", name='ck_ingestion_status'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ingestion_jobs_status'), 'ingestion_jobs', ['status'], unique=False)
    op.create_index("uq_ingestion_singleton", "ingestion_jobs", [sa.text("(true)")], unique=True)
    op.create_table('source_gate',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('next_request_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('blocked', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('worker_heartbeat',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_table('catalog_embeddings',
        sa.Column('record_id', sa.BigInteger(), nullable=False),
        sa.Column('vector', pgvector.sqlalchemy.vector.VECTOR(dim=4096), nullable=False),
        sa.Column('catalog_fingerprint', sa.String(), nullable=False),
        sa.Column('input_fingerprint', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('model_digest', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['record_id'], ['archival_records.id'], ),
        sa.PrimaryKeyConstraint('record_id')
    )
    op.create_table('logical_images',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('record_id', sa.BigInteger(), nullable=False),
        sa.Column('identity', sa.String(), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('front_reference', sa.String(), nullable=False),
        sa.Column('back_reference', sa.String(), nullable=True),
        sa.Column('front_url', sa.String(), nullable=True),
        sa.Column('back_url', sa.String(), nullable=True),
        sa.Column('reference_fingerprint', sa.String(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['record_id'], ['archival_records.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('record_id', 'identity')
    )
    op.create_index(op.f('ix_logical_images_record_id'), 'logical_images', ['record_id'], unique=False)
    op.create_table('ingestion_tasks',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('job_id', sa.BigInteger(), nullable=False),
        sa.Column('task_key', sa.String(), nullable=False),
        sa.Column('stage', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('photo_id', sa.BigInteger(), nullable=True),
        sa.Column('image_id', sa.BigInteger(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('claim_token', sa.String(), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['image_id'], ['logical_images.id'], ),
        sa.ForeignKeyConstraint(['job_id'], ['ingestion_jobs.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'task_key')
    )
    op.create_index(op.f('ix_ingestion_tasks_job_id'), 'ingestion_tasks', ['job_id'], unique=False)
    op.create_index('ix_task_claim', 'ingestion_tasks', ['job_id', 'status', 'available_at', 'id'], unique=False)
    op.create_table('vision_descriptions',
        sa.Column('image_id', sa.BigInteger(), nullable=False),
        sa.Column('description', sa.String(), nullable=False),
        sa.Column('lexical', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('model_digest', sa.String(), nullable=False),
        sa.Column('prompt_version', sa.String(), nullable=False),
        sa.Column('reference_fingerprint', sa.String(), nullable=False),
        sa.Column('content_fingerprint', sa.String(), nullable=False),
        sa.Column('input_fingerprint', sa.String(), nullable=False),
        sa.Column('catalog_input_fingerprint', sa.String(), server_default='', nullable=False),
        sa.Column('catalog_extraction', postgresql.JSONB(), server_default='{}', nullable=False),
        sa.ForeignKeyConstraint(['image_id'], ['logical_images.id'], ),
        sa.PrimaryKeyConstraint('image_id')
    )
    op.create_table('vision_embeddings',
        sa.Column('image_id', sa.BigInteger(), nullable=False),
        sa.Column('vector', pgvector.sqlalchemy.vector.VECTOR(dim=4096), nullable=False),
        sa.Column('input_fingerprint', sa.String(), nullable=False),
        sa.Column('model', sa.String(), nullable=False),
        sa.Column('model_digest', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['image_id'], ['logical_images.id'], ),
        sa.PrimaryKeyConstraint('image_id')
    )
    op.create_table('ingestion_attempts',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('task_id', sa.BigInteger(), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('error_code', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['task_id'], ['ingestion_tasks.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'number')
    )
    op.create_index(op.f('ix_ingestion_attempts_task_id'), 'ingestion_attempts', ['task_id'], unique=False)
    op.execute("CREATE INDEX ix_catalog_fts ON archival_records USING gin (to_tsvector('simple', lexical))")
    op.execute("CREATE INDEX ix_vision_fts ON vision_descriptions USING gin (to_tsvector('simple', lexical))")
    op.execute("CREATE INDEX ix_catalog_trgm ON archival_records USING gin (lexical gin_trgm_ops)")
    op.execute("CREATE INDEX ix_vision_trgm ON vision_descriptions USING gin (lexical gin_trgm_ops)")
    op.create_table("chat_conversations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("chat_turns",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("conversation_id", sa.Text(), sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("conversation_id", "request_id"),
        sa.UniqueConstraint("conversation_id", "sequence"))
    op.create_index("ix_chat_turns_conversation_id", "chat_turns", ["conversation_id"])
    # A new installation starts with one paused process and its initial tasks.
    op.execute("""
        WITH job AS (
            INSERT INTO ingestion_jobs (source, status, scope)
            VALUES ('live', 'paused', jsonb_build_object('source_client_id', gen_random_uuid()::text)) RETURNING id
        )
        INSERT INTO ingestion_tasks (job_id, task_key, stage, payload, status, attempts)
        SELECT id, 'count', 'count', '{}'::jsonb, 'queued', 0 FROM job
        UNION ALL
        SELECT id, 'page:1', 'discovery', jsonb_build_object('page', 1), 'queued', 0 FROM job
    """)


def downgrade():
    # Dropping a baseline removes all application data; restore backups instead
    # when recovering an existing installation. Keep shared extensions installed.
    op.drop_table("chat_turns")
    op.drop_table("chat_conversations")
    op.drop_table("ingestion_attempts")
    op.drop_table("vision_embeddings")
    op.drop_table("vision_descriptions")
    op.drop_table("ingestion_tasks")
    op.drop_table("logical_images")
    op.drop_table("catalog_embeddings")
    op.drop_table("worker_heartbeat")
    op.drop_table("source_gate")
    op.drop_table("ingestion_jobs")
    op.drop_table("chat_waiters")
    op.drop_table("archival_records")
