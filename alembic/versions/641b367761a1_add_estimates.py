"""add estimates

Creates the estimate and estimate_items tables backing priced proposals and
the customer's per-line approval workflow.

The money columns are guarded by check constraints so an out-of-range total
cannot reach the database even if a future caller bypasses the service layer.

Revision ID: 641b367761a1
Revises: c500515ba7a6
Create Date: 2026-10-01 15:52:18.921445

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "641b367761a1"
down_revision: str | None = 'c500515ba7a6'
branch_labels: str | None = None
depends_on: str | None = None


ESTIMATE_STATUSES = (
    "DRAFT",
    "SENT",
    "PARTIALLY_APPROVED",
    "APPROVED",
    "DECLINED",
    "EXPIRED",
    "CANCELLED",
)
ESTIMATE_ITEM_STATUSES = ("PENDING", "APPROVED", "DECLINED")
ESTIMATE_ITEM_TYPES = ("LABOR", "PART", "SERVICE", "FEE", "DISCOUNT")


def upgrade() -> None:
    op.create_table('estimates',
    sa.Column('estimate_number', sa.String(length=30), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('vehicle_id', sa.UUID(), nullable=False),
    sa.Column('inspection_id', sa.UUID(), nullable=True),
    sa.Column('service_request_id', sa.UUID(), nullable=True),
    sa.Column('created_by_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('valid_until', sa.Date(), nullable=True),
    sa.Column('subtotal', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('discount_amount', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('tax_rate', sa.Numeric(precision=6, scale=4, asdecimal=False), nullable=False),
    sa.Column('tax_amount', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('total', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('customer_notes', sa.Text(), nullable=True),
    sa.Column('decline_reason', sa.Text(), nullable=True),
    sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['inspection_id'], ['inspections.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['service_request_id'], ['service_requests.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint(
        "status IN " + str(ESTIMATE_STATUSES),
        name="ck_estimates_status",
    ),
    sa.CheckConstraint(
        "subtotal >= 0 AND discount_amount >= 0 AND tax_amount >= 0 AND total >= 0",
        name="ck_estimates_money_non_negative",
    ),
    sa.CheckConstraint(
        "tax_rate >= 0 AND tax_rate <= 1",
        name="ck_estimates_tax_rate",
    ),
    )
    op.create_index(op.f('ix_estimates_created_by_id'), 'estimates', ['created_by_id'], unique=False)
    op.create_index(op.f('ix_estimates_customer_id'), 'estimates', ['customer_id'], unique=False)
    op.create_index(op.f('ix_estimates_estimate_number'), 'estimates', ['estimate_number'], unique=True)
    op.create_index(op.f('ix_estimates_inspection_id'), 'estimates', ['inspection_id'], unique=False)
    op.create_index(op.f('ix_estimates_service_request_id'), 'estimates', ['service_request_id'], unique=False)
    op.create_index(op.f('ix_estimates_status'), 'estimates', ['status'], unique=False)
    op.create_index(op.f('ix_estimates_vehicle_id'), 'estimates', ['vehicle_id'], unique=False)
    op.create_table('estimate_items',
    sa.Column('estimate_id', sa.UUID(), nullable=False),
    sa.Column('item_type', sa.String(length=20), nullable=False),
    sa.Column('description', sa.String(length=300), nullable=False),
    sa.Column('sequence', sa.Integer(), nullable=False),
    sa.Column('labor_hours', sa.Numeric(precision=8, scale=2, asdecimal=False), nullable=True),
    sa.Column('labor_rate', sa.Numeric(precision=10, scale=2, asdecimal=False), nullable=True),
    sa.Column('part_number', sa.String(length=50), nullable=True),
    sa.Column('part_name', sa.String(length=200), nullable=True),
    sa.Column('quantity', sa.Numeric(precision=10, scale=2, asdecimal=False), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('discount_amount', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('line_total', sa.Numeric(precision=12, scale=2, asdecimal=False), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('customer_notes', sa.Text(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('is_optional', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['estimate_id'], ['estimates.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.CheckConstraint(
        "status IN " + str(ESTIMATE_ITEM_STATUSES),
        name="ck_estimate_items_status",
    ),
    sa.CheckConstraint(
        "item_type IN " + str(ESTIMATE_ITEM_TYPES),
        name="ck_estimate_items_type",
    ),
    sa.CheckConstraint(
        "quantity > 0 AND unit_price >= 0 AND discount_amount >= 0",
        name="ck_estimate_items_money",
    ),
    )
    op.create_index(op.f('ix_estimate_items_estimate_id'), 'estimate_items', ['estimate_id'], unique=False)
    op.create_index(op.f('ix_estimate_items_status'), 'estimate_items', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_estimate_items_status'), table_name='estimate_items')
    op.drop_index(op.f('ix_estimate_items_estimate_id'), table_name='estimate_items')
    op.drop_table('estimate_items')
    op.drop_index(op.f('ix_estimates_vehicle_id'), table_name='estimates')
    op.drop_index(op.f('ix_estimates_status'), table_name='estimates')
    op.drop_index(op.f('ix_estimates_service_request_id'), table_name='estimates')
    op.drop_index(op.f('ix_estimates_inspection_id'), table_name='estimates')
    op.drop_index(op.f('ix_estimates_estimate_number'), table_name='estimates')
    op.drop_index(op.f('ix_estimates_customer_id'), table_name='estimates')
    op.drop_index(op.f('ix_estimates_created_by_id'), table_name='estimates')
    op.drop_table('estimates')
    # ### end Alembic commands ###
