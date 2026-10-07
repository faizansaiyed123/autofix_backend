"""Permission definitions for role-based access control (RBAC).

This module defines all permission constants and role-to-permission mappings.
The seed script uses these same constants to populate the database.
"""

from __future__ import annotations

from enum import Enum


class RoleEnum(str, Enum):
    OWNER = "OWNER"
    SERVICE_ADVISOR = "SERVICE_ADVISOR"
    TECHNICIAN = "TECHNICIAN"
    PARTS_STAFF = "PARTS_STAFF"
    CUSTOMER = "CUSTOMER"


class PermissionEnum(str, Enum):
    # User management
    USERS_READ = "users:read"
    USERS_WRITE = "users:write"
    USERS_MANAGE = "users:manage"

    # Customer management
    CUSTOMERS_READ = "customers:read"
    CUSTOMERS_WRITE = "customers:write"
    CUSTOMERS_MANAGE = "customers:manage"

    # Vehicle management
    VEHICLES_READ = "vehicles:read"
    VEHICLES_WRITE = "vehicles:write"
    VEHICLES_MANAGE = "vehicles:manage"

    # Appointment management
    APPOINTMENTS_READ = "appointments:read"
    APPOINTMENTS_WRITE = "appointments:write"
    APPOINTMENTS_MANAGE = "appointments:manage"

    # Service requests
    SERVICE_REQUESTS_READ = "service_requests:read"
    SERVICE_REQUESTS_WRITE = "service_requests:write"

    # Check-ins
    CHECK_INS_READ = "check_ins:read"
    CHECK_INS_WRITE = "check_ins:write"

    # Inspections
    INSPECTIONS_READ = "inspections:read"
    INSPECTIONS_WRITE = "inspections:write"

    # Estimates
    ESTIMATES_READ = "estimates:read"
    ESTIMATES_WRITE = "estimates:write"
    ESTIMATES_APPROVE = "estimates:approve"

    # Repair orders
    REPAIR_ORDERS_READ = "repair_orders:read"
    REPAIR_ORDERS_WRITE = "repair_orders:write"
    REPAIR_ORDERS_MANAGE = "repair_orders:manage"

    # Tasks / Labor
    TASKS_READ = "tasks:read"
    TASKS_WRITE = "tasks:write"
    LABOR_READ = "labor:read"
    LABOR_WRITE = "labor:write"

    # Quality control
    QC_READ = "qc:read"
    QC_PERFORM = "qc:perform"

    # Parts / Inventory
    PARTS_READ = "parts:read"
    PARTS_WRITE = "parts:write"
    PARTS_MANAGE = "parts:manage"
    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"
    INVENTORY_MANAGE = "inventory:manage"

    # Suppliers / Purchase orders
    SUPPLIERS_READ = "suppliers:read"
    SUPPLIERS_WRITE = "suppliers:write"
    PURCHASE_ORDERS_READ = "purchase_orders:read"
    PURCHASE_ORDERS_WRITE = "purchase_orders:write"

    # Part requests
    PART_REQUESTS_READ = "part_requests:read"
    PART_REQUESTS_WRITE = "part_requests:write"
    PART_REQUEST_APPROVE = "part_requests:approve"

    # Invoicing / Payments
    INVOICES_READ = "invoices:read"
    INVOICES_WRITE = "invoices:write"
    INVOICES_MANAGE = "invoices:manage"
    PAYMENTS_READ = "payments:read"
    PAYMENTS_WRITE = "payments:write"
    # Taking money back off an invoice. Held by staff, never by the customer:
    # a payer may settle a bill, but only the shop decides what leaves the till.
    PAYMENTS_REFUND = "payments:refund"

    # Notifications
    NOTIFICATIONS_READ = "notifications:read"

    # Feedback
    FEEDBACK_READ = "feedback:read"
    FEEDBACK_WRITE = "feedback:write"

    # Reports / Analytics
    # Split deliberately. `reports:read` is the shop's own operational numbers —
    # what it took in, what is open on the floor, what is low on the shelf — and
    # every member of staff needs those. `reports:analytics` is the management
    # view: technicians ranked against each other, customers ranked by what they
    # are worth. Those are instruments for judging named people, so they are
    # held by the OWNER alone (the OWNER row grants every permission) and are
    # deliberately absent from SERVICE_ADVISOR.
    REPORTS_READ = "reports:read"
    REPORTS_ANALYTICS = "reports:analytics"

    # Settings
    SETTINGS_MANAGE = "settings:manage"

    # Audit logs
    AUDIT_LOGS_READ = "audit_logs:read"


# Permission sets for each role
ROLE_PERMISSIONS: dict[RoleEnum, list[PermissionEnum]] = {
    RoleEnum.OWNER: [
        # Owner gets ALL permissions
        *list(PermissionEnum),
    ],
    RoleEnum.SERVICE_ADVISOR: [
        PermissionEnum.USERS_READ,
        PermissionEnum.CUSTOMERS_READ,
        PermissionEnum.CUSTOMERS_WRITE,
        PermissionEnum.VEHICLES_READ,
        PermissionEnum.VEHICLES_WRITE,
        PermissionEnum.APPOINTMENTS_READ,
        PermissionEnum.APPOINTMENTS_WRITE,
        PermissionEnum.APPOINTMENTS_MANAGE,
        PermissionEnum.SERVICE_REQUESTS_READ,
        PermissionEnum.SERVICE_REQUESTS_WRITE,
        PermissionEnum.CHECK_INS_READ,
        PermissionEnum.CHECK_INS_WRITE,
        PermissionEnum.INSPECTIONS_READ,
        PermissionEnum.INSPECTIONS_WRITE,
        PermissionEnum.ESTIMATES_READ,
        PermissionEnum.ESTIMATES_WRITE,
        PermissionEnum.ESTIMATES_APPROVE,
        PermissionEnum.REPAIR_ORDERS_READ,
        PermissionEnum.REPAIR_ORDERS_WRITE,
        PermissionEnum.REPAIR_ORDERS_MANAGE,
        PermissionEnum.TASKS_READ,
        PermissionEnum.TASKS_WRITE,
        PermissionEnum.LABOR_READ,
        PermissionEnum.LABOR_WRITE,
        PermissionEnum.QC_READ,
        PermissionEnum.QC_PERFORM,
        PermissionEnum.PARTS_READ,
        PermissionEnum.INVENTORY_READ,
        PermissionEnum.PART_REQUEST_APPROVE,
        PermissionEnum.INVOICES_READ,
        PermissionEnum.INVOICES_WRITE,
        PermissionEnum.INVOICES_MANAGE,
        PermissionEnum.PAYMENTS_READ,
        PermissionEnum.PAYMENTS_WRITE,
        PermissionEnum.PAYMENTS_REFUND,
        PermissionEnum.NOTIFICATIONS_READ,
        PermissionEnum.FEEDBACK_READ,
        PermissionEnum.REPORTS_READ,
        PermissionEnum.VEHICLES_MANAGE,
        PermissionEnum.CUSTOMERS_MANAGE,
    ],
    RoleEnum.TECHNICIAN: [
        PermissionEnum.VEHICLES_READ,
        PermissionEnum.APPOINTMENTS_READ,
        PermissionEnum.SERVICE_REQUESTS_READ,
        PermissionEnum.CHECK_INS_READ,
        PermissionEnum.INSPECTIONS_READ,
        PermissionEnum.INSPECTIONS_WRITE,
        PermissionEnum.REPAIR_ORDERS_READ,
        PermissionEnum.REPAIR_ORDERS_WRITE,
        PermissionEnum.TASKS_READ,
        PermissionEnum.TASKS_WRITE,
        PermissionEnum.LABOR_READ,
        PermissionEnum.LABOR_WRITE,
        PermissionEnum.QC_READ,
        PermissionEnum.QC_PERFORM,
        PermissionEnum.PARTS_READ,
        PermissionEnum.INVENTORY_READ,
        PermissionEnum.PART_REQUESTS_READ,
        PermissionEnum.PART_REQUESTS_WRITE,
        PermissionEnum.NOTIFICATIONS_READ,
        PermissionEnum.REPORTS_READ,
    ],
    RoleEnum.PARTS_STAFF: [
        PermissionEnum.PARTS_READ,
        PermissionEnum.PARTS_WRITE,
        PermissionEnum.PARTS_MANAGE,
        PermissionEnum.INVENTORY_READ,
        PermissionEnum.INVENTORY_WRITE,
        PermissionEnum.INVENTORY_MANAGE,
        PermissionEnum.SUPPLIERS_READ,
        PermissionEnum.SUPPLIERS_WRITE,
        PermissionEnum.PURCHASE_ORDERS_READ,
        PermissionEnum.PURCHASE_ORDERS_WRITE,
        PermissionEnum.PART_REQUEST_APPROVE,
        PermissionEnum.PART_REQUESTS_READ,
        PermissionEnum.NOTIFICATIONS_READ,
        PermissionEnum.REPORTS_READ,
    ],
    RoleEnum.CUSTOMER: [
        PermissionEnum.VEHICLES_READ,
        PermissionEnum.APPOINTMENTS_READ,
        PermissionEnum.APPOINTMENTS_WRITE,
        PermissionEnum.SERVICE_REQUESTS_READ,
        PermissionEnum.SERVICE_REQUESTS_WRITE,
        PermissionEnum.INSPECTIONS_READ,
        PermissionEnum.ESTIMATES_READ,
        PermissionEnum.ESTIMATES_APPROVE,
        PermissionEnum.REPAIR_ORDERS_READ,
        # Read-only on money owed: a customer may look at what they are billed
        # and pay it, but only the shop writes the bill. `invoices:write` and
        # `invoices:manage` are deliberately absent — a customer who could raise
        # or void an invoice would be issuing their own statement.
        PermissionEnum.INVOICES_READ,
        PermissionEnum.PAYMENTS_READ,
        PermissionEnum.PAYMENTS_WRITE,
        PermissionEnum.NOTIFICATIONS_READ,
        PermissionEnum.FEEDBACK_READ,
        PermissionEnum.FEEDBACK_WRITE,
    ],
}
