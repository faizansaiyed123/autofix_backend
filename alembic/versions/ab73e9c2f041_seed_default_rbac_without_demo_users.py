"""Provision baseline RBAC roles and permissions without creating demo accounts."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.common.ids import uuid7

revision: str = "ab73e9c2f041"
down_revision: str | None = "f8c4d6207b91"
branch_labels: str | None = None
depends_on: str | None = None


ROLES = (
    "OWNER",
    "SERVICE_ADVISOR",
    "TECHNICIAN",
    "PARTS_STAFF",
    "CUSTOMER",
)

PERMISSIONS = (
    "users:read",
    "users:write",
    "users:manage",
    "customers:read",
    "customers:write",
    "customers:manage",
    "vehicles:read",
    "vehicles:write",
    "vehicles:manage",
    "appointments:read",
    "appointments:write",
    "appointments:manage",
    "service_requests:read",
    "service_requests:write",
    "check_ins:read",
    "check_ins:write",
    "inspections:read",
    "inspections:write",
    "estimates:read",
    "estimates:write",
    "estimates:approve",
    "repair_orders:read",
    "repair_orders:write",
    "repair_orders:manage",
    "tasks:read",
    "tasks:write",
    "labor:read",
    "labor:write",
    "qc:read",
    "qc:perform",
    "parts:read",
    "parts:write",
    "parts:manage",
    "inventory:read",
    "inventory:write",
    "inventory:manage",
    "suppliers:read",
    "suppliers:write",
    "purchase_orders:read",
    "purchase_orders:write",
    "part_requests:read",
    "part_requests:write",
    "part_requests:approve",
    "invoices:read",
    "invoices:write",
    "invoices:manage",
    "payments:read",
    "payments:write",
    "payments:refund",
    "notifications:read",
    "feedback:read",
    "feedback:write",
    "reports:read",
    "reports:analytics",
    "settings:manage",
    "audit_logs:read",
)

CUSTOMER_PERMISSIONS = (
    "vehicles:read",
    "appointments:read",
    "appointments:write",
    "service_requests:read",
    "service_requests:write",
    "inspections:read",
    "estimates:read",
    "estimates:approve",
    "repair_orders:read",
    "invoices:read",
    "payments:read",
    "payments:write",
    "notifications:read",
    "feedback:read",
    "feedback:write",
)

SERVICE_ADVISOR_PERMISSIONS = (
    "users:read",
    "customers:read",
    "customers:write",
    "vehicles:read",
    "vehicles:write",
    "appointments:read",
    "appointments:write",
    "appointments:manage",
    "service_requests:read",
    "service_requests:write",
    "check_ins:read",
    "check_ins:write",
    "inspections:read",
    "inspections:write",
    "estimates:read",
    "estimates:write",
    "estimates:approve",
    "repair_orders:read",
    "repair_orders:write",
    "repair_orders:manage",
    "tasks:read",
    "tasks:write",
    "labor:read",
    "labor:write",
    "qc:read",
    "qc:perform",
    "parts:read",
    "inventory:read",
    "part_requests:approve",
    "invoices:read",
    "invoices:write",
    "invoices:manage",
    "payments:read",
    "payments:write",
    "payments:refund",
    "notifications:read",
    "feedback:read",
    "reports:read",
    "vehicles:manage",
    "customers:manage",
)

TECHNICIAN_PERMISSIONS = (
    "vehicles:read",
    "appointments:read",
    "service_requests:read",
    "check_ins:read",
    "inspections:read",
    "inspections:write",
    "repair_orders:read",
    "repair_orders:write",
    "tasks:read",
    "tasks:write",
    "labor:read",
    "labor:write",
    "qc:read",
    "qc:perform",
    "parts:read",
    "inventory:read",
    "part_requests:read",
    "part_requests:write",
    "notifications:read",
    "reports:read",
)

PARTS_STAFF_PERMISSIONS = (
    "parts:read",
    "parts:write",
    "parts:manage",
    "inventory:read",
    "inventory:write",
    "inventory:manage",
    "suppliers:read",
    "suppliers:write",
    "purchase_orders:read",
    "purchase_orders:write",
    "part_requests:approve",
    "part_requests:read",
    "notifications:read",
    "reports:read",
)

ROLE_PERMISSION_MAP = {
    "SERVICE_ADVISOR": SERVICE_ADVISOR_PERMISSIONS,
    "TECHNICIAN": TECHNICIAN_PERMISSIONS,
    "PARTS_STAFF": PARTS_STAFF_PERMISSIONS,
    "CUSTOMER": CUSTOMER_PERMISSIONS,
}


def upgrade() -> None:
    connection = op.get_bind()

    created_roles: set[str] = set()
    for name in ROLES:
        exists = connection.execute(
            sa.text("SELECT 1 FROM roles WHERE name = :name LIMIT 1"),
            {"name": name},
        ).first()
        if not exists:
            connection.execute(
                sa.text(
                    "INSERT INTO roles (id, name, description) "
                    "VALUES (:id, :name, :description)"
                ),
                {
                    "id": uuid7(),
                    "name": name,
                    "description": f"{name} role",
                },
            )
            created_roles.add(name)

    for name in PERMISSIONS:
        exists = connection.execute(
            sa.text("SELECT 1 FROM permissions WHERE name = :name LIMIT 1"),
            {"name": name},
        ).first()
        if not exists:
            connection.execute(
                sa.text(
                    "INSERT INTO permissions (id, name, description) "
                    "VALUES (:id, :name, :description)"
                ),
                {
                    "id": uuid7(),
                    "name": name,
                    "description": name.replace("_", " ").replace(":", " "),
                },
            )

    mapping_count = connection.execute(
        sa.text("SELECT count(*) FROM role_permissions")
    ).scalar_one()

    role_ids = dict(
        connection.execute(sa.text("SELECT name, id FROM roles")).all()
    )
    permission_ids = dict(
        connection.execute(sa.text("SELECT name, id FROM permissions")).all()
    )
    mapping_rows = []
    if mapping_count == 0:
        mapping_rows.extend(
            (role_ids["OWNER"], permission_ids[permission])
            for permission in PERMISSIONS
        )
        for role_name, names in ROLE_PERMISSION_MAP.items():
            if role_name == "CUSTOMER":
                continue
            mapping_rows.extend(
                (role_ids[role_name], permission_ids[permission])
                for permission in names
            )

    # A newly created customer role must be usable by the public portal even
    # if an existing database already has customized mappings for staff roles.
    if mapping_count == 0 or "CUSTOMER" in created_roles:
        mapping_rows.extend(
            (role_ids["CUSTOMER"], permission_ids[permission])
            for permission in CUSTOMER_PERMISSIONS
        )

    for role_id, permission_id in mapping_rows:
        connection.execute(
            sa.text(
                "INSERT INTO role_permissions (id, role_id, permission_id) "
                "VALUES (:id, :role_id, :permission_id) "
                "ON CONFLICT (role_id, permission_id) DO NOTHING"
            ),
            {
                "id": uuid7(),
                "role_id": role_id,
                "permission_id": permission_id,
            },
        )


def downgrade() -> None:
    # Deliberately preserve these baseline role and permission rows so accounts
    # that already reference them are not orphaned by a schema rollback.
    pass
