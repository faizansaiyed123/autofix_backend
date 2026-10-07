"""Add service requests

Revision ID: a34de2084a81
Revises: 48297fa50c4e
Create Date: 2026-09-20 16:01:56.426216

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a34de2084a81"
down_revision: str | None = '48297fa50c4e'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table('service_requests',
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('vehicle_id', sa.UUID(), nullable=True),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('priority', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('service_advisor_notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_service_requests_customer_id'), 'service_requests', ['customer_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_service_requests_customer_id'), table_name='service_requests')
    op.drop_table('service_requests')
