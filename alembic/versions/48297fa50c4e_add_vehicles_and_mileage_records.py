"""Add vehicles and mileage records

Revision ID: 48297fa50c4e
Revises: 9c3bf43debcc
Create Date: 2026-09-20 15:35:02.038389

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "48297fa50c4e"
down_revision: str | None = '9c3bf43debcc'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table('vehicles',
    sa.Column('vin', sa.String(length=17), nullable=True),
    sa.Column('license_plate', sa.String(length=20), nullable=True),
    sa.Column('make', sa.String(length=50), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=False),
    sa.Column('year', sa.Integer(), nullable=True),
    sa.Column('trim', sa.String(length=100), nullable=True),
    sa.Column('engine', sa.String(length=100), nullable=True),
    sa.Column('transmission', sa.String(length=50), nullable=True),
    sa.Column('mileage', sa.Integer(), nullable=True),
    sa.Column('color', sa.String(length=30), nullable=True),
    sa.Column('fuel_type', sa.String(length=20), nullable=True),
    sa.Column('purchase_date', sa.String(), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('customer_id', sa.UUID(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_vehicles_customer_id'), 'vehicles', ['customer_id'], unique=False)
    op.create_index(op.f('ix_vehicles_license_plate'), 'vehicles', ['license_plate'], unique=False)
    op.create_index(op.f('ix_vehicles_vin'), 'vehicles', ['vin'], unique=True)
    op.create_table('vehicle_mileage_records',
    sa.Column('vehicle_id', sa.UUID(), nullable=False),
    sa.Column('mileage', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['vehicle_id'], ['vehicles.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_vehicle_mileage_records_vehicle_id'), 'vehicle_mileage_records', ['vehicle_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_vehicle_mileage_records_vehicle_id'), table_name='vehicle_mileage_records')
    op.drop_table('vehicle_mileage_records')
    op.drop_index(op.f('ix_vehicles_vin'), table_name='vehicles')
    op.drop_index(op.f('ix_vehicles_license_plate'), table_name='vehicles')
    op.drop_index(op.f('ix_vehicles_customer_id'), table_name='vehicles')
    op.drop_table('vehicles')
