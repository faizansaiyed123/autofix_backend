"""add inspections

Creates the digital vehicle inspection tables.

Table order matters here: ``inspection_photos`` references
``inspection_items``, so items are created first. An earlier draft of this
migration also gave ``inspection_items`` a ``photo_id`` column pointing back
at ``inspection_photos``, which made the two tables mutually dependent and
left the migration unrunnable on a fresh database. Photos now reference
items in one direction only.

Revision ID: e0ff351c4e01
Revises: 02e02fc5e0dc
Create Date: 2026-09-20 16:43:42.392921
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e0ff351c4e01"
down_revision: str | None = "02e02fc5e0dc"
branch_labels: str | None = None
depends_on: str | None = None


ITEM_STATUSES = ("GOOD", "ATTENTION", "RECOMMENDED", "URGENT", "NOT_CHECKED")
INSPECTION_STATUSES = ("DRAFT", "IN_PROGRESS", "COMPLETED", "CANCELLED")


def upgrade() -> None:
    op.create_table(
        "inspections",
        sa.Column("vehicle_id", sa.UUID(), nullable=False),
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("checkin_id", sa.UUID(), nullable=True),
        sa.Column("technician_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("mileage", sa.Integer(), nullable=True),
        sa.Column("overall_notes", sa.Text(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["checkin_id"], ["check_ins.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["technician_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN " + str(INSPECTION_STATUSES),
            name="ck_inspections_status",
        ),
        sa.CheckConstraint("mileage IS NULL OR mileage >= 0", name="ck_inspections_mileage"),
    )
    op.create_index("ix_inspections_customer_id", "inspections", ["customer_id"])
    op.create_index("ix_inspections_vehicle_id", "inspections", ["vehicle_id"])
    op.create_index("ix_inspections_technician_id", "inspections", ["technician_id"])
    op.create_index("ix_inspections_status", "inspections", ["status"])

    op.create_table(
        "inspection_items",
        sa.Column("inspection_id", sa.UUID(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("item_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("measurement", sa.String(length=100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("recommendation", sa.String(length=20), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["inspection_id"], ["inspections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN " + str(ITEM_STATUSES),
            name="ck_inspection_items_status",
        ),
    )
    op.create_index("ix_inspection_items_category", "inspection_items", ["category"])
    op.create_index("ix_inspection_items_inspection_id", "inspection_items", ["inspection_id"])

    op.create_table(
        "inspection_photos",
        sa.Column("inspection_id", sa.UUID(), nullable=False),
        sa.Column("inspection_item_id", sa.UUID(), nullable=True),
        sa.Column("photo_url", sa.String(length=500), nullable=False),
        sa.Column("caption", sa.String(length=200), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["inspection_id"], ["inspections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["inspection_item_id"], ["inspection_items.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inspection_photos_inspection_id", "inspection_photos", ["inspection_id"])
    op.create_index(
        "ix_inspection_photos_inspection_item_id",
        "inspection_photos",
        ["inspection_item_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inspection_photos_inspection_item_id", table_name="inspection_photos")
    op.drop_index("ix_inspection_photos_inspection_id", table_name="inspection_photos")
    op.drop_table("inspection_photos")
    op.drop_index("ix_inspection_items_inspection_id", table_name="inspection_items")
    op.drop_index("ix_inspection_items_category", table_name="inspection_items")
    op.drop_table("inspection_items")
    op.drop_index("ix_inspections_status", table_name="inspections")
    op.drop_index("ix_inspections_technician_id", table_name="inspections")
    op.drop_index("ix_inspections_vehicle_id", table_name="inspections")
    op.drop_index("ix_inspections_customer_id", table_name="inspections")
    op.drop_table("inspections")
