"""Configure embedding dimensions for an empty index without discarding vectors."""
from alembic import op
import sqlalchemy as sa

from core.config import settings

revision = "0002_embedding_dimensions"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def resize_empty_indexes(dimensions: int):
    connection = op.get_bind()
    tables = ("catalog_embeddings", "vision_embeddings")
    # Hold both tables until the migration transaction commits.
    op.execute("LOCK TABLE catalog_embeddings, vision_embeddings IN ACCESS EXCLUSIVE MODE")
    changes = []
    for table in tables:
        actual = connection.scalar(sa.text("""
            SELECT format_type(atttypid, atttypmod) FROM pg_attribute
            WHERE attrelid = to_regclass(:table) AND attname = 'vector' AND NOT attisdropped
        """), {"table": table})
        if actual != f"vector({dimensions})":
            if connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
                raise RuntimeError("Cannot change embedding dimensions with existing vectors. "
                                   "Keep EMBEDDING_PROVIDER/EMBEDDING_DIMENSIONS unchanged, or use "
                                   "a new database and rebuild the index. No vectors were deleted.")
            changes.append(table)
    for table in changes:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN vector TYPE vector({dimensions})")


def upgrade():
    resize_empty_indexes(settings.embedding_dimensions)


def downgrade():
    resize_empty_indexes(4096)
