"""Add check-ins

Revision ID: 02e02fc5e0dc
Revises: a34de2084a81
Create Date: 2026-09-20 16:20:19.184920

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "02e02fc5e0dc"
down_revision: str | None = 'a34de2084a81'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table('check_ins',
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('vehicle_id', sa.UUID(), nullable=False),
    sa.Column('odometer', sa.Integer(), nullable=False),
    sa.Column('checkin_type', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('service_advisor_id', sa.UUID(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('expected_completion', sa.String(), nullable=True),
    sa.Column('tire_condition', sa.String(length=50), nullable=True),
    sa.Column('fluid_levels', sa.String(length=50), nullable=True),
    sa.Column('lights_status', sa.String(length=50), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['service_advisor_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_check_ins_customer_id'), 'check_ins', ['customer_id'], unique=False)
    op.create_index(op.f('ix_check_ins_service_advisor_id'), 'check_ins', ['service_advisor_id'], unique=False)
    op.create_index(op.f('ix_check_ins_vehicle_id'), 'check_ins', ['vehicle_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_check_ins_vehicle_id'), table_name='check_ins')
    op.drop_index(op.f('ix_check_ins_service_advisor_id'), table_name='check_ins')
    op.drop_index(op.f('ix_check_ins_customer_id'), table_name='check_ins')
    op.drop_table('check_ins')
