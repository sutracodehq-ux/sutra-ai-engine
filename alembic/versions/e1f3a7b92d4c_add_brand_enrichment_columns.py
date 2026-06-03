"""add brand enrichment columns

Revision ID: e1f3a7b92d4c
Revises: c80bc972b19c
Create Date: 2026-06-03
"""
from alembic import op
import sqlalchemy as sa

revision = "e1f3a7b92d4c"
down_revision = "c80bc972b19c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("website_url", sa.String(512), nullable=True))
    op.add_column("tenants", sa.Column("brand_enriched_at", sa.DateTime, nullable=True))
    op.add_column("tenants", sa.Column("re_enrich_interval_days", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "re_enrich_interval_days")
    op.drop_column("tenants", "brand_enriched_at")
    op.drop_column("tenants", "website_url")
