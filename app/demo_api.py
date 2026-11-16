"""Isolated AutoFix demo API.

Run with: uvicorn app.demo_api:app --reload --port 8001

This module deliberately does not import the production app, database, models,
settings, migrations, email worker, or seed routines. All records are synthetic
and kept in process memory. It is a demonstration surface, not a production API.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Request, Response

DEMO_PASSWORD = "demo1234"
USERS: list[dict[str, Any]] = [
    {"id": "demo-owner", "email": "owner@autofix.demo", "password": DEMO_PASSWORD, "first_name": "John", "last_name": "Smith", "phone": "+1 555 010 1001", "is_active": True, "is_staff": True, "email_verified": True, "roles": ["OWNER"]},
    {"id": "demo-advisor", "email": "manager@autofix.demo", "password": DEMO_PASSWORD, "first_name": "Sarah", "last_name": "Johnson", "phone": "+1 555 010 1002", "is_active": True, "is_staff": True, "email_verified": True, "roles": ["SERVICE_ADVISOR"]},
    {"id": "demo-tech", "email": "tech@autofix.demo", "password": DEMO_PASSWORD, "first_name": "Mike", "last_name": "Rodriguez", "phone": "+1 555 010 1003", "is_active": True, "is_staff": True, "email_verified": True, "roles": ["TECHNICIAN"]},
    {"id": "demo-parts", "email": "parts@autofix.demo", "password": DEMO_PASSWORD, "first_name": "Lisa", "last_name": "Chen", "phone": "+1 555 010 1004", "is_active": True, "is_staff": True, "email_verified": True, "roles": ["PARTS_STAFF"]},
    {"id": "demo-customer", "email": "customer@autofix.demo", "password": DEMO_PASSWORD, "first_name": "David", "last_name": "Wilson", "phone": "+1 555 010 1005", "is_active": True, "is_staff": False, "email_verified": True, "roles": ["CUSTOMER"]},
]

OWNER_PERMISSIONS = [
    "reports:read", "reports:analytics", "notifications:read",
    "service_requests:read", "service_requests:create", "service_requests:update",
    "appointments:read", "appointments:create", "appointments:update",
    "customers:read", "customers:create", "customers:update",
    "vehicles:read", "vehicles:create", "vehicles:update",
    "check_ins:read", "check_ins:create", "check_ins:update",
    "inspections:read", "inspections:create", "inspections:update",
    "estimates:read", "estimates:create", "estimates:update",
    "repair_orders:read", "repair_orders:create", "repair_orders:update",
    "part_requests:read", "part_requests:create", "part_requests:update",
    "qc:read", "qc:create", "qc:update", "labor:read", "labor:create", "labor:update",
    "invoices:read", "invoices:create", "payments:read", "payments:create",
    "parts:read", "parts:create", "parts:update",
    "inventory:read", "inventory:create",
    "suppliers:read", "suppliers:create", "suppliers:update",
    "purchase_orders:read", "purchase_orders:create", "purchase_orders:update",
    "users:read", "users:create", "users:update", "audit_logs:read",
]
PERMISSIONS: dict[str, list[str]] = {
    "OWNER": OWNER_PERMISSIONS,
    "SERVICE_ADVISOR": [p for p in OWNER_PERMISSIONS if p not in {
        "reports:analytics", "inventory:read", "users:create", "users:update", "audit_logs:read"
    }],
    "TECHNICIAN": [
        "notifications:read", "inspections:read", "inspections:create", "inspections:update",
        "repair_orders:read", "repair_orders:update", "labor:read", "labor:create",
        "part_requests:read", "part_requests:create", "qc:read", "qc:update", "vehicles:read",
    ],
    "PARTS_STAFF": [
        "notifications:read", "part_requests:read", "part_requests:update",
        "parts:read", "parts:create", "parts:update", "inventory:read", "inventory:create",
        "suppliers:read", "suppliers:create", "suppliers:update",
        "purchase_orders:read", "purchase_orders:create", "purchase_orders:update",
    ],
    "CUSTOMER": [],
}

COLLECTIONS = {
    "customers": "customers",
    "vehicles": "vehicles",
    "service_requests": "service_requests",
    "appointments": "appointments",
    "check_ins": "check_ins",
    "inspections": "inspections",
    "estimates": "estimates",
    "repair_orders": "repair_orders",
    "labor": "labor",
    "part_requests": "part_requests",
    "qc": "quality_checks",
    "invoices": "invoices",
    "payments": "payments",
    "parts": "parts",
    "inventory": "inventory_transactions",
    "suppliers": "suppliers",
    "purchase_orders": "purchase_orders",
    "users": "users",
    "audit": "audit_logs",
}
READ_PERMISSION = {
    "customers": "customers:read", "vehicles": "vehicles:read",
    "service_requests": "service_requests:read", "appointments": "appointments:read",
    "check_ins": "check_ins:read", "inspections": "inspections:read",
    "estimates": "estimates:read", "repair_orders": "repair_orders:read",
    "labor": "labor:read", "part_requests": "part_requests:read", "qc": "qc:read",
    "invoices": "invoices:read", "payments": "payments:read", "parts": "parts:read",
    "inventory": "inventory:read", "suppliers": "suppliers:read",
    "purchase_orders": "purchase_orders:read", "users": "users:read",
    "audit": "audit_logs:read",
}
WRITE_PERMISSION = {
    "customers": "customers:create", "vehicles": "vehicles:create",
    "service_requests": "service_requests:create", "appointments": "appointments:create",
    "check_ins": "check_ins:create", "inspections": "inspections:create",
    "estimates": "estimates:create", "repair_orders": "repair_orders:create",
    "labor": "labor:create", "part_requests": "part_requests:create", "qc": "qc:create",
    "invoices": "invoices:create", "payments": "payments:create", "parts": "parts:create",
    "inventory": "inventory:create", "suppliers": "suppliers:create",
    "purchase_orders": "purchase_orders:create", "users": "users:create",
    "audit": "audit_logs:read",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(days: int = 0, hour: int = 10) -> str:
    value = _now() + timedelta(days=days)
    return value.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat()


def _day(days: int = 0) -> str:
    return _iso(days)[:10]


def _seed_state() -> dict[str, list[dict[str, Any]]]:
    customers = [
        {"id": "customer-1", "first_name": "David", "last_name": "Wilson", "company_name": None, "email": "customer@autofix.demo", "phone": "+1 555 010 1005", "preferred_contact": "EMAIL", "customer_status": "ACTIVE", "notes": "Long-standing customer", "user_id": "demo-customer", "addresses": []},
        {"id": "customer-2", "first_name": "Olivia", "last_name": "Martinez", "company_name": None, "email": "olivia.martinez@example.test", "phone": "+1 555 010 2002", "preferred_contact": "PHONE", "customer_status": "ACTIVE", "notes": None, "user_id": None, "addresses": []},
        {"id": "customer-3", "first_name": "Noah", "last_name": "Patel", "company_name": "Patel Design Studio", "email": "noah@example.test", "phone": "+1 555 010 2003", "preferred_contact": "EMAIL", "customer_status": "ACTIVE", "notes": "Fleet customer", "user_id": None, "addresses": []},
        {"id": "customer-4", "first_name": "Emma", "last_name": "Thompson", "company_name": None, "email": "emma@example.test", "phone": "+1 555 010 2004", "preferred_contact": "SMS", "customer_status": "ACTIVE", "notes": None, "user_id": None, "addresses": []},
    ]
    vehicles = [
        {"id": "vehicle-1", "customer_id": "customer-1", "vin": "1HGBH41JXMN109186", "license_plate": "AUT-104", "make": "Toyota", "model": "Camry", "year": 2022, "trim": "SE", "engine": "2.5L I4", "transmission": "Automatic", "mileage": 42850, "color": "Silver", "fuel_type": "Petrol", "purchase_date": "2022-04-12", "notes": None, "status": "IN_SHOP", "mileage_records": []},
        {"id": "vehicle-2", "customer_id": "customer-2", "vin": "1FTFW1E58NFA12345", "license_plate": "AUT-205", "make": "Ford", "model": "F-150", "year": 2021, "trim": "XLT", "engine": "3.5L V6", "transmission": "Automatic", "mileage": 61200, "color": "Blue", "fuel_type": "Petrol", "purchase_date": "2021-02-17", "notes": None, "status": "ACTIVE", "mileage_records": []},
        {"id": "vehicle-3", "customer_id": "customer-3", "vin": "WBA8E9G55GNU12345", "license_plate": "AUT-306", "make": "BMW", "model": "330i", "year": 2020, "trim": "Sport", "engine": "2.0L Turbo", "transmission": "Automatic", "mileage": 73410, "color": "Black", "fuel_type": "Petrol", "purchase_date": "2020-06-20", "notes": None, "status": "ACTIVE", "mileage_records": []},
        {"id": "vehicle-4", "customer_id": "customer-4", "vin": "1N4BL4BV0NC123456", "license_plate": "AUT-407", "make": "Nissan", "model": "Altima", "year": 2023, "trim": "SV", "engine": "2.5L I4", "transmission": "CVT", "mileage": 21800, "color": "White", "fuel_type": "Petrol", "purchase_date": "2023-01-10", "notes": None, "status": "ACTIVE", "mileage_records": []},
    ]
    service_requests = [
        {"id": "request-1", "customer_id": "customer-1", "vehicle_id": "vehicle-1", "title": "Brake vibration at highway speed", "description": "Steering wheel vibrates under braking. Inspect the front rotors.", "priority": "HIGH", "status": "IN_REVIEW", "service_advisor_notes": "Inspect front brakes", "created_at": _iso(-1), "updated_at": _iso(-1)},
        {"id": "request-2", "customer_id": "customer-2", "vehicle_id": "vehicle-2", "title": "Oil service and tire rotation", "description": "Routine 60,000 mile service.", "priority": "NORMAL", "status": "APPROVED", "service_advisor_notes": None, "created_at": _iso(-3), "updated_at": _iso(-2)},
        {"id": "request-3", "customer_id": "customer-4", "vehicle_id": "vehicle-4", "title": "Air conditioning not cooling", "description": "Cabin takes a long time to cool down.", "priority": "NORMAL", "status": "NEW", "service_advisor_notes": None, "created_at": _iso(-2), "updated_at": _iso(-2)},
    ]
    appointments = [
        {"id": "appointment-1", "customer_id": "customer-1", "vehicle_id": "vehicle-1", "service_request_id": "request-1", "service_type": "Brake inspection", "scheduled_start": _iso(0, 9), "scheduled_end": _iso(0, 11), "duration_minutes": 120, "advisor_id": "demo-advisor", "technician_id": "demo-tech", "bay": "Bay 2", "customer_concern": "Brake vibration", "notes": "Road test after inspection", "status": "CONFIRMED", "cancellation_reason": None, "checkin_id": "checkin-1", "created_at": _iso(-2), "updated_at": _iso(-1)},
        {"id": "appointment-2", "customer_id": "customer-2", "vehicle_id": "vehicle-2", "service_request_id": "request-2", "service_type": "Scheduled maintenance", "scheduled_start": _iso(1, 10), "scheduled_end": _iso(1, 12), "duration_minutes": 120, "advisor_id": "demo-advisor", "technician_id": "demo-tech", "bay": "Bay 1", "customer_concern": "60k service", "notes": None, "status": "CONFIRMED", "cancellation_reason": None, "checkin_id": None, "created_at": _iso(-2), "updated_at": _iso(-2)},
        {"id": "appointment-3", "customer_id": "customer-3", "vehicle_id": "vehicle-3", "service_request_id": None, "service_type": "Engine diagnostic", "scheduled_start": _iso(-1, 13), "scheduled_end": _iso(-1, 15), "duration_minutes": 120, "advisor_id": "demo-advisor", "technician_id": "demo-tech", "bay": "Bay 3", "customer_concern": "Check engine light", "notes": None, "status": "COMPLETED", "cancellation_reason": None, "checkin_id": None, "created_at": _iso(-5), "updated_at": _iso(-1)},
    ]
    check_ins = [
        {"id": "checkin-1", "vehicle_id": "vehicle-1", "customer_id": "customer-1", "odometer": 42850, "checkin_type": "APPOINTMENT", "status": "IN_PROGRESS", "notes": "Customer reports vibration while braking", "expected_completion": _iso(0, 17), "tire_condition": "GOOD", "fluid_levels": "GOOD", "lights_status": "GOOD", "service_advisor_id": "demo-advisor", "created_at": _iso(0, 9), "updated_at": _iso(0, 9)},
        {"id": "checkin-2", "vehicle_id": "vehicle-2", "customer_id": "customer-2", "odometer": 61200, "checkin_type": "APPOINTMENT", "status": "COMPLETED", "notes": None, "expected_completion": _iso(1, 15), "tire_condition": "FAIR", "fluid_levels": "GOOD", "lights_status": "GOOD", "service_advisor_id": "demo-advisor", "created_at": _iso(-1, 10), "updated_at": _iso(-1, 10)},
    ]
    inspections = [
        {"id": "inspection-1", "vehicle_id": "vehicle-1", "customer_id": "customer-1", "checkin_id": "checkin-1", "technician_id": "demo-tech", "mileage": 42850, "overall_notes": "Front brake components require attention.", "status": "IN_PROGRESS", "overall_condition": "FAIR", "items": [
            {"id": "inspection-item-1", "category": "Brakes", "item_name": "Front brake pads", "status": "NEEDS_ATTENTION", "severity_color": "AMBER", "measurement": "3 mm", "notes": "Wear approaching limit", "recommendation": "Replace front brake pads", "photo_url": None, "photo_caption": None, "photos": []},
            {"id": "inspection-item-2", "category": "Tires", "item_name": "Front tires", "status": "GOOD", "severity_color": "GREEN", "measurement": "6 mm", "notes": None, "recommendation": None, "photo_url": None, "photo_caption": None, "photos": []},
        ], "created_at": _iso(0, 9), "updated_at": _iso(0, 9)},
        {"id": "inspection-2", "vehicle_id": "vehicle-2", "customer_id": "customer-2", "checkin_id": "checkin-2", "technician_id": "demo-tech", "mileage": 61200, "overall_notes": "Routine service inspection complete.", "status": "COMPLETED", "overall_condition": "GOOD", "items": [
            {"id": "inspection-item-3", "category": "Engine", "item_name": "Engine oil", "status": "GOOD", "severity_color": "GREEN", "measurement": "Level OK", "notes": None, "recommendation": None, "photo_url": None, "photo_caption": None, "photos": []},
        ], "created_at": _iso(-1), "updated_at": _iso(-1)},
    ]
    estimates = [
        {"id": "estimate-1", "estimate_number": "EST-2026-0042", "customer_id": "customer-1", "vehicle_id": "vehicle-1", "inspection_id": "inspection-1", "service_request_id": "request-1", "created_by_id": "demo-advisor", "status": "SENT", "subtotal": 485.0, "discount_amount": 0.0, "tax_rate": 0.08, "tax_amount": 38.8, "total": 523.8, "approved_total": 0.0, "valid_until": _day(14), "is_expired": False, "notes": "Brake system service", "customer_notes": "Please approve the recommended repairs.", "decline_reason": None, "sent_at": _iso(-1), "decided_at": None, "items": [
            {"id": "estimate-item-1", "item_type": "LABOR", "description": "Front brake service", "labor_hours": 1.5, "labor_rate": 110.0, "part_number": None, "part_name": None, "quantity": 1, "unit_price": 165.0, "discount_amount": 0.0, "is_optional": False, "notes": None, "status": "PENDING", "line_total": 165.0, "customer_notes": None, "created_at": _iso(-1), "updated_at": _iso(-1)},
            {"id": "estimate-item-2", "item_type": "PART", "description": "Ceramic brake pad set", "labor_hours": None, "labor_rate": None, "part_number": "BRK-204", "part_name": "Ceramic brake pads", "quantity": 1, "unit_price": 320.0, "discount_amount": 0.0, "is_optional": False, "notes": None, "status": "PENDING", "line_total": 320.0, "customer_notes": None, "created_at": _iso(-1), "updated_at": _iso(-1)},
        ], "created_at": _iso(-1), "updated_at": _iso(-1)},
        {"id": "estimate-2", "estimate_number": "EST-2026-0038", "customer_id": "customer-2", "vehicle_id": "vehicle-2", "inspection_id": None, "service_request_id": "request-2", "created_by_id": "demo-advisor", "status": "APPROVED", "subtotal": 690.0, "discount_amount": 0.0, "tax_rate": 0.08, "tax_amount": 55.2, "total": 745.2, "approved_total": 745.2, "valid_until": _day(8), "is_expired": False, "notes": None, "customer_notes": None, "decline_reason": None, "sent_at": _iso(-4), "decided_at": _iso(-3), "items": [
            {"id": "estimate-item-3", "item_type": "LABOR", "description": "60,000 mile maintenance", "labor_hours": 2.0, "labor_rate": 120.0, "part_number": None, "part_name": None, "quantity": 1, "unit_price": 240.0, "discount_amount": 0.0, "is_optional": False, "notes": None, "status": "APPROVED", "line_total": 240.0, "customer_notes": None, "created_at": _iso(-4), "updated_at": _iso(-3)},
            {"id": "estimate-item-4", "item_type": "PART", "description": "Oil and filter kit", "labor_hours": None, "labor_rate": None, "part_number": "OIL-510", "part_name": "Synthetic oil service kit", "quantity": 1, "unit_price": 450.0, "discount_amount": 0.0, "is_optional": False, "notes": None, "status": "APPROVED", "line_total": 450.0, "customer_notes": None, "created_at": _iso(-4), "updated_at": _iso(-3)},
        ], "created_at": _iso(-4), "updated_at": _iso(-3)},
    ]
    repair_orders = [
        {"id": "repair-1", "ro_number": "RO-2026-0192", "customer_id": "customer-1", "vehicle_id": "vehicle-1", "estimate_id": "estimate-1", "appointment_id": "appointment-1", "advisor_id": "demo-advisor", "technician_id": "demo-tech", "status": "IN_PROGRESS", "odometer_in": 42850, "odometer_out": None, "bay": "Bay 2", "promised_at": _iso(0, 17), "notes": "Verify brake vibration after repair", "customer_notes": None, "cancel_reason": None, "started_at": _iso(0, 9), "completed_at": None, "delivered_at": None, "all_tasks_done": False, "tasks": [
            {"id": "task-1", "repair_order_id": "repair-1", "description": "Inspect front brake rotors", "notes": None, "status": "IN_PROGRESS", "sequence": 1, "assigned_to_id": "demo-tech", "completed_at": None},
            {"id": "task-2", "repair_order_id": "repair-1", "description": "Replace front brake pads", "notes": None, "status": "PENDING", "sequence": 2, "assigned_to_id": "demo-tech", "completed_at": None},
        ], "created_at": _iso(0), "updated_at": _iso(0)},
        {"id": "repair-2", "ro_number": "RO-2026-0188", "customer_id": "customer-2", "vehicle_id": "vehicle-2", "estimate_id": "estimate-2", "appointment_id": "appointment-2", "advisor_id": "demo-advisor", "technician_id": "demo-tech", "status": "AWAITING_QC", "odometer_in": 61200, "odometer_out": 61205, "bay": "Bay 1", "promised_at": _iso(1, 16), "notes": None, "customer_notes": None, "cancel_reason": None, "started_at": _iso(-1), "completed_at": None, "delivered_at": None, "all_tasks_done": True, "tasks": [
            {"id": "task-3", "repair_order_id": "repair-2", "description": "Complete scheduled maintenance", "notes": None, "status": "COMPLETED", "sequence": 1, "assigned_to_id": "demo-tech", "completed_at": _iso(0)},
        ], "created_at": _iso(-1), "updated_at": _iso(0)},
    ]
    parts = [
        {"id": "part-1", "part_number": "BRK-204", "name": "Ceramic brake pad set", "description": "Front axle ceramic brake pads", "category": "Brakes", "brand": "RoadPro", "unit_cost": 128.0, "unit_price": 320.0, "quantity_on_hand": 4, "reorder_level": 6, "location": "A-01", "status": "ACTIVE"},
        {"id": "part-2", "part_number": "OIL-510", "name": "Synthetic oil service kit", "description": "5W-30 oil and filter kit", "category": "Fluids", "brand": "MotorWorks", "unit_cost": 110.0, "unit_price": 225.0, "quantity_on_hand": 18, "reorder_level": 8, "location": "B-03", "status": "ACTIVE"},
        {"id": "part-3", "part_number": "FLT-110", "name": "Engine air filter", "description": "OEM-fit engine air filter", "category": "Filters", "brand": "AirFlow", "unit_cost": 18.0, "unit_price": 42.0, "quantity_on_hand": 2, "reorder_level": 5, "location": "B-01", "status": "ACTIVE"},
        {"id": "part-4", "part_number": "BRK-390", "name": "Brake rotor", "description": "Vented front brake rotor", "category": "Brakes", "brand": "RoadPro", "unit_cost": 92.0, "unit_price": 185.0, "quantity_on_hand": 0, "reorder_level": 4, "location": "A-02", "status": "ACTIVE"},
        {"id": "part-5", "part_number": "BAT-021", "name": "AGM battery", "description": "12V AGM battery", "category": "Electrical", "brand": "VoltMax", "unit_cost": 142.0, "unit_price": 265.0, "quantity_on_hand": 7, "reorder_level": 4, "location": "C-02", "status": "ACTIVE"},
    ]
    suppliers = [
        {"id": "supplier-1", "name": "Metro Auto Supply", "contact_name": "Alex Rivera", "email": "orders@metroauto.example.test", "phone": "+1 555 020 1001", "address_line1": "18 Industrial Way", "address_line2": None, "address": "18 Industrial Way, Austin, TX", "city": "Austin", "state": "TX", "postal_code": "78701", "country": "USA", "account_number": "MAS-204", "website": None, "lead_time_days": 2, "payment_terms": "NET 30", "notes": None, "status": "ACTIVE", "is_preferred": True, "is_active": True},
        {"id": "supplier-2", "name": "Northside Parts Co.", "contact_name": "Morgan Lee", "email": "sales@northside.example.test", "phone": "+1 555 020 1002", "address_line1": "230 Commerce St", "address_line2": None, "address": "230 Commerce St, Austin, TX", "city": "Austin", "state": "TX", "postal_code": "78702", "country": "USA", "account_number": None, "website": None, "lead_time_days": 4, "payment_terms": "NET 15", "notes": None, "status": "ACTIVE", "is_preferred": False, "is_active": True},
    ]
    part_requests = [
        {"id": "part-request-1", "repair_order_id": "repair-1", "part_id": "part-1", "requested_by_id": "demo-tech", "approved_by_id": None, "fulfilled_by_id": None, "quantity": 1, "status": "PENDING", "reason": "Required for approved brake repair", "decision_reason": None, "created_at": _iso(0), "updated_at": _iso(0)},
        {"id": "part-request-2", "repair_order_id": "repair-2", "part_id": "part-2", "requested_by_id": "demo-tech", "approved_by_id": "demo-parts", "fulfilled_by_id": "demo-parts", "quantity": 1, "status": "FULFILLED", "reason": None, "decision_reason": None, "created_at": _iso(-2), "updated_at": _iso(-1)},
    ]
    quality_checks = [
        {"id": "qc-1", "repair_order_id": "repair-2", "inspector_id": "demo-advisor", "status": "IN_PROGRESS", "attempt_number": 1, "started_at": _iso(0), "completed_at": None, "notes": "Final road test and brake check", "failure_reason": None, "is_terminal": False, "passed": False, "checks": [
            {"id": "qc-check-1", "check_type": "ROAD_TEST", "label": "Road test", "passed": False, "blocking": True, "notes": None, "manually_overridden": False}
        ], "photos": []},
    ]
    invoices = [
        {"id": "invoice-1", "invoice_number": "INV-2026-0114", "customer_id": "customer-1", "vehicle_id": "vehicle-1", "repair_order_id": "repair-1", "estimate_id": "estimate-1", "created_by_id": "demo-advisor", "status": "PARTIALLY_PAID", "invoice_date": _day(-6), "due_date": _day(8), "subtotal": 690.0, "discount_amount": 0.0, "tax_rate": 0.08, "tax_amount": 55.2, "total": 745.2, "amount_paid": 300.0, "balance": 445.2, "is_overdue": False, "days_overdue": 0, "void_reason": None, "issued_at": _iso(-6), "paid_at": None, "voided_at": None, "notes": None, "customer_notes": "Thank you for choosing AutoFix.", "items": []},
        {"id": "invoice-2", "invoice_number": "INV-2026-0108", "customer_id": "customer-2", "vehicle_id": "vehicle-2", "repair_order_id": "repair-2", "estimate_id": None, "created_by_id": "demo-advisor", "status": "ISSUED", "invoice_date": _day(-20), "due_date": _day(-2), "subtotal": 480.0, "discount_amount": 0.0, "tax_rate": 0.08, "tax_amount": 38.4, "total": 518.4, "amount_paid": 0.0, "balance": 518.4, "is_overdue": True, "days_overdue": 2, "void_reason": None, "issued_at": _iso(-20), "paid_at": None, "voided_at": None, "notes": None, "customer_notes": None, "items": []},
        {"id": "invoice-3", "invoice_number": "INV-2026-0099", "customer_id": "customer-3", "vehicle_id": "vehicle-3", "repair_order_id": "repair-2", "estimate_id": None, "created_by_id": "demo-advisor", "status": "PAID", "invoice_date": _day(-30), "due_date": _day(-15), "subtotal": 250.0, "discount_amount": 0.0, "tax_rate": 0.08, "tax_amount": 20.0, "total": 270.0, "amount_paid": 270.0, "balance": 0.0, "is_overdue": False, "days_overdue": 0, "void_reason": None, "issued_at": _iso(-30), "paid_at": _iso(-29), "voided_at": None, "notes": None, "customer_notes": None, "items": []},
    ]
    payments = [
        {"id": "payment-1", "invoice_id": "invoice-1", "amount": 300.0, "method": "CARD", "status": "COMPLETED", "payment_date": _iso(-2), "reference": "CARD-2048", "notes": None, "recorded_by_id": "demo-advisor", "void_reason": None, "voided_at": None},
        {"id": "payment-2", "invoice_id": "invoice-3", "amount": 270.0, "method": "CARD", "status": "COMPLETED", "payment_date": _iso(-29), "reference": "CARD-1993", "notes": None, "recorded_by_id": "demo-advisor", "void_reason": None, "voided_at": None},
    ]
    inventory_transactions = [
        {"id": "txn-1", "part_id": "part-1", "transaction_type": "RECEIPT", "quantity": 12, "quantity_before": 0, "quantity_after": 12, "unit_cost": 128.0, "reference": "OPENING-STOCK", "notes": "Opening stock", "created_at": _iso(-12)},
        {"id": "txn-2", "part_id": "part-1", "transaction_type": "ISSUE", "quantity": 8, "quantity_before": 12, "quantity_after": 4, "unit_cost": 128.0, "reference": "RO-2026-0192", "notes": "Brake repair", "created_at": _iso(-2)},
    ]
    purchase_orders = [
        {"id": "po-1", "po_number": "PO-2026-0034", "supplier_id": "supplier-1", "supplier_name": "Metro Auto Supply", "status": "SENT", "order_date": _day(-1), "expected_delivery_date": _day(2), "subtotal": 460.0, "tax_amount": 36.8, "shipping_amount": 0.0, "total_amount": 496.8, "currency": "USD", "notes": "Replenish service stock", "internal_notes": None, "sent_at": _iso(-1), "received_at": None, "cancelled_at": None, "cancel_reason": None, "item_count": 2, "total_units_ordered": 5, "total_units_received": 0, "is_fully_received": False, "is_terminal": False, "is_editable": False, "is_overdue": False, "items": [
            {"id": "po-item-1", "part_id": "part-3", "part_number": "FLT-110", "part_name": "Engine air filter", "quantity_ordered": 3, "quantity_received": 0, "unit_cost": 18.0, "line_total": 54.0},
            {"id": "po-item-2", "part_id": "part-4", "part_number": "BRK-390", "part_name": "Brake rotor", "quantity_ordered": 2, "quantity_received": 0, "unit_cost": 92.0, "line_total": 184.0},
        ]},
    ]
    labor = [
        {"id": "labor-1", "repair_order_id": "repair-1", "repair_task_id": "task-1", "technician_id": "demo-tech", "description": "Brake system diagnosis", "actual_hours": 0.8, "billable_hours": 0.8, "hourly_rate": 110.0, "labor_cost": 88.0, "performed_at": _iso(0), "notes": "Confirmed front brake vibration"},
    ]
    notifications = [
        {"id": "notification-1", "user_id": "demo-owner", "title": "Estimate awaiting customer decision", "message": "EST-2026-0042 is waiting for approval.", "notification_type": "ESTIMATE", "priority": "HIGH", "is_read": False, "created_at": _iso(-1), "entity_type": "estimate", "entity_id": "estimate-1"},
        {"id": "notification-2", "user_id": "demo-advisor", "title": "Customer service request received", "message": "A new air-conditioning request needs review.", "notification_type": "SERVICE_REQUEST", "priority": "NORMAL", "is_read": False, "created_at": _iso(-2), "entity_type": "service_request", "entity_id": "request-3"},
        {"id": "notification-3", "user_id": "demo-tech", "title": "Repair task assigned", "message": "Inspect front brake rotors on RO-2026-0192.", "notification_type": "REPAIR_ORDER", "priority": "NORMAL", "is_read": False, "created_at": _iso(0), "entity_type": "repair_order", "entity_id": "repair-1"},
    ]
    audit_logs = [
        {"id": "audit-1", "action": "STATUS_CHANGED", "entity_type": "repair_order", "entity_id": "repair-1", "entity_label": "RO-2026-0192", "actor_id": "demo-advisor", "actor_email": "manager@autofix.demo", "actor_role": "SERVICE_ADVISOR", "actor_label": "Sarah Johnson", "summary": "Repair order moved to In progress", "changes": None, "created_at": _iso(0)},
    ]
    users = [{k: v for k, v in user.items() if k != "password"} for user in USERS]
    return {
        "customers": customers, "vehicles": vehicles, "service_requests": service_requests,
        "appointments": appointments, "check_ins": check_ins, "inspections": inspections,
        "estimates": estimates, "repair_orders": repair_orders, "parts": parts,
        "suppliers": suppliers, "part_requests": part_requests, "quality_checks": quality_checks,
        "invoices": invoices, "payments": payments, "inventory_transactions": inventory_transactions,
        "purchase_orders": purchase_orders, "labor": labor, "notifications": notifications,
        "audit_logs": audit_logs, "users": users, "mileage_records": [],
    }


STATE = _seed_state()
NEXT_ID = 100


def reset_demo_state() -> None:
    """Restore the synthetic in-memory dataset; primarily useful to isolated tests."""
    global STATE, NEXT_ID
    STATE = _seed_state()
    NEXT_ID = 100


def _new_id(prefix: str) -> str:
    global NEXT_ID
    NEXT_ID += 1
    return f"{prefix}-{NEXT_ID}"


def _user(request: Request) -> dict[str, Any]:
    authorization = request.headers.get("authorization", "")
    if not authorization.lower().startswith("bearer demo-access:"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    email = unquote(authorization[len("Bearer demo-access:"):])
    person = next((u for u in USERS if u["email"].lower() == email.lower()), None)
    if person is None:
        raise HTTPException(status_code=401, detail="Invalid demo session")
    return person


def _safe_user(person: dict[str, Any]) -> dict[str, Any]:
    return {k: deepcopy(v) for k, v in person.items() if k != "password"}


def _permissions(person: dict[str, Any]) -> list[str]:
    return PERMISSIONS.get(person["roles"][0], [])


def _require(person: dict[str, Any], permission: str) -> None:
    if permission not in _permissions(person):
        raise HTTPException(status_code=403, detail="Not permitted in this demo role")


def _require_staff(person: dict[str, Any]) -> None:
    if not person["is_staff"]:
        raise HTTPException(status_code=403, detail="Staff access required")


def _get(collection: str, item_id: str) -> dict[str, Any]:
    item = next((x for x in STATE[collection] if str(x.get("id")) == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return item


def _copy(value: Any) -> Any:
    return deepcopy(value)


def _paged(rows: list[dict[str, Any]], query: dict[str, str]) -> dict[str, Any]:
    result = list(rows)
    search = (query.get("search") or query.get("q") or "").lower()
    if search:
        result = [row for row in result if search in str(row).lower()]
    ignored = {"page", "size", "limit", "search", "q", "sort", "order"}
    for key, value in query.items():
        if key not in ignored and value and any(key in row for row in result):
            result = [row for row in result if str(row.get(key)) == value]
    page = max(1, int(query.get("page", "1")))
    size = max(1, min(100, int(query.get("size", query.get("limit", "20")))))
    start = (page - 1) * size
    return {"data": _copy(result[start:start + size]), "meta": {
        "page": page, "size": size, "total": len(result),
        "pages": max(1, (len(result) + size - 1) // size),
    }}


def _status_view(status: str) -> dict[str, Any]:
    labels = {
        "NEW": "Received", "IN_REVIEW": "Under review", "APPROVED": "Approved",
        "REJECTED": "Declined", "CONFIRMED": "Confirmed", "IN_PROGRESS": "In progress",
        "COMPLETED": "Completed", "SENT": "Waiting for you", "PAID": "Paid",
        "PARTIALLY_PAID": "Part paid", "ISSUED": "Payment due",
        "READY_FOR_PICKUP": "Ready for pickup",
    }
    return {"label": labels.get(status, status.replace("_", " ").lower()), "detail": None, "tone": "neutral"}


def _customer_for(person: dict[str, Any]) -> dict[str, Any]:
    customer = next((c for c in STATE["customers"] if c.get("user_id") == person["id"] or c.get("email") == person["email"]), None)
    if customer is None:
        raise HTTPException(status_code=404, detail="No customer account is linked to this user")
    return customer


def _audit(person: dict[str, Any], action: str, entity: str, item: dict[str, Any]) -> None:
    label = item.get("estimate_number") or item.get("ro_number") or item.get("invoice_number") or item.get("name") or item.get("title") or item["id"]
    STATE["audit_logs"].insert(0, {
        "id": _new_id("audit"), "action": action, "entity_type": entity,
        "entity_id": item["id"], "entity_label": label, "actor_id": person["id"],
        "actor_email": person["email"], "actor_role": person["roles"][0],
        "actor_label": person["first_name"] + " " + person["last_name"],
        "summary": action.replace("_", " ").lower(), "changes": None, "created_at": _now().isoformat(),
    })


def _create(collection: str, prefix: str, payload: dict[str, Any], person: dict[str, Any], entity: str) -> dict[str, Any]:
    item = _copy(payload)
    item.update({"id": _new_id(prefix), "created_at": _now().isoformat(), "updated_at": _now().isoformat()})
    STATE[collection].insert(0, item)
    _audit(person, "CREATED", entity, item)
    return _copy(item)


def _dashboard(person: dict[str, Any]) -> dict[str, Any]:
    invoices = STATE["invoices"]
    outstanding = sum(float(i["balance"]) for i in invoices if i["status"] != "VOID")
    overdue = [i for i in invoices if i.get("is_overdue") and i["balance"] > 0]
    work = STATE["repair_orders"]
    return {
        "period": {"start_date": _day(-29), "end_date": _day(), "days": 30},
        "generated_for": person["email"],
        "revenue": {"invoiced_total": sum(float(i["total"]) for i in invoices if i["status"] != "VOID"), "collected_total": sum(float(p["amount"]) for p in STATE["payments"] if p["status"] == "COMPLETED"), "outstanding_balance": round(outstanding, 2), "overdue_balance": round(sum(float(i["balance"]) for i in overdue), 2), "overdue_count": len(overdue)},
        "work": {"repair_orders_open": len(work), "repair_orders_in_progress": sum(x["status"] == "IN_PROGRESS" for x in work), "repair_orders_on_hold": sum(x["status"] == "ON_HOLD" for x in work), "repair_orders_awaiting_qc": sum(x["status"] == "AWAITING_QC" for x in work), "completed_in_period": sum(x["status"] == "COMPLETED" for x in work), "part_requests_pending": sum(x["status"] == "PENDING" for x in STATE["part_requests"])},
        "money": {"invoices_draft": sum(x["status"] == "DRAFT" for x in invoices), "invoices_issued": sum(x["status"] == "ISSUED" for x in invoices), "invoices_partially_paid": sum(x["status"] == "PARTIALLY_PAID" for x in invoices), "invoices_paid": sum(x["status"] == "PAID" for x in invoices)},
        "operations": {"low_stock_count": sum(0 < p["quantity_on_hand"] <= p["reorder_level"] for p in STATE["parts"]), "out_of_stock_count": sum(p["quantity_on_hand"] == 0 for p in STATE["parts"]), "inventory_cost_value": round(sum(p["quantity_on_hand"] * p["unit_cost"] for p in STATE["parts"]), 2), "appointments_today": sum(x["scheduled_start"][:10] == _day() for x in STATE["appointments"]), "vehicles_in_shop": sum(x["status"] == "IN_SHOP" for x in STATE["vehicles"]), "pending_part_requests": sum(x["status"] == "PENDING" for x in STATE["part_requests"]), "unread_notifications": sum(x["user_id"] == person["id"] and not x["is_read"] for x in STATE["notifications"])},
    }


def _portal(person: dict[str, Any], subpath: list[str], method: str, body: dict[str, Any]) -> Any:
    customer = _customer_for(person)
    cid = customer["id"]
    vehicles = [x for x in STATE["vehicles"] if x["customer_id"] == cid]
    appointments = [x for x in STATE["appointments"] if x["customer_id"] == cid]
    requests = [x for x in STATE["service_requests"] if x["customer_id"] == cid]
    estimates = [x for x in STATE["estimates"] if x["customer_id"] == cid]
    invoices = [x for x in STATE["invoices"] if x["customer_id"] == cid]
    sub = "/".join(subpath)

    if not sub and method == "GET":
        balance = sum(float(x["balance"]) for x in invoices)
        next_appointment = None
        upcoming = sorted((x for x in appointments if x["status"] not in {"CANCELLED", "COMPLETED"}), key=lambda x: x["scheduled_start"])
        if upcoming:
            a = _copy(upcoming[0])
            v = next((x for x in vehicles if x["id"] == a["vehicle_id"]), None)
            a["vehicle_label"] = " ".join(str(v.get(k)) for k in ("year", "make", "model") if v and v.get(k))
            a["status_view"] = _status_view(a["status"])
            next_appointment = a
        return {"customer_id": cid, "full_name": customer["first_name"] + " " + customer["last_name"], "email": customer["email"], "phone": customer["phone"], "vehicle_count": len(vehicles), "open_balance": round(balance, 2), "overdue_balance": round(sum(float(x["balance"]) for x in invoices if x.get("is_overdue")), 2), "awaiting_approval": sum(sum(line["status"] == "PENDING" for line in e["items"]) for e in estimates), "awaiting_payment": sum(x["balance"] > 0 for x in invoices), "ready_for_pickup": sum(x["status"] == "READY_FOR_PICKUP" for x in STATE["repair_orders"] if x["customer_id"] == cid), "next_appointment": next_appointment}
    if sub == "vehicles" and method == "GET":
        return [{k: v for k, v in item.items() if k in {"id", "make", "model", "year", "color", "license_plate", "vin", "mileage", "status"}} | {"status_view": _status_view(item["status"])} for item in vehicles]
    if len(subpath) == 2 and subpath[0] == "vehicles" and method == "GET":
        vehicle = next((v for v in vehicles if v["id"] == subpath[1]), None)
        if vehicle is None:
            raise HTTPException(status_code=404, detail="Vehicle not found")
        return {"vehicle": _copy(vehicle), "service_history": [], "appointments": _copy(appointments)}
    if sub == "appointments" and method == "GET":
        return [_copy(x) | {"vehicle_label": next((v["make"] + " " + v["model"] for v in vehicles if v["id"] == x["vehicle_id"]), "Vehicle"), "status_view": _status_view(x["status"])} for x in appointments]
    if sub == "appointments" and method == "POST":
        if not any(v["id"] == body.get("vehicle_id") for v in vehicles):
            raise HTTPException(status_code=422, detail="Choose one of your vehicles")
        start = body.get("scheduled_start") or _iso(1)
        item = _create("appointments", "appointment", {"customer_id": cid, "vehicle_id": body["vehicle_id"], "service_request_id": None, "service_type": body.get("service_type", "General service"), "scheduled_start": start, "scheduled_end": start, "duration_minutes": int(body.get("duration_minutes", 60)), "advisor_id": None, "technician_id": None, "bay": None, "customer_concern": body.get("customer_concern"), "notes": None, "status": "REQUESTED", "cancellation_reason": None, "checkin_id": None}, person, "appointment")
        return item | {"vehicle_label": "Vehicle", "status_view": _status_view(item["status"])}
    if sub == "service-requests" and method == "GET":
        return [_copy(x) | {"status_view": _status_view(x["status"])} for x in requests]
    if sub == "service-requests" and method == "POST":
        vehicle_id = body.get("vehicle_id")
        if vehicle_id and not any(v["id"] == vehicle_id for v in vehicles):
            raise HTTPException(status_code=404, detail="Vehicle not found")
        item = _create("service_requests", "request", {"customer_id": cid, "vehicle_id": vehicle_id, "title": body.get("title", "Service request"), "description": body.get("description"), "priority": body.get("priority", "NORMAL"), "status": "NEW", "service_advisor_notes": None}, person, "service_request")
        return item | {"status_view": _status_view(item["status"])}
    if sub == "estimates" and method == "GET":
        return [_copy(x) | {"status_view": _status_view(x["status"])} for x in estimates]
    if len(subpath) == 2 and subpath[0] == "estimates" and method == "GET":
        estimate = next((e for e in estimates if e["id"] == subpath[1]), None)
        if estimate is None:
            raise HTTPException(status_code=404, detail="Estimate not found")
        return _copy(estimate) | {"status_view": _status_view(estimate["status"])}
    if len(subpath) == 5 and subpath[0] == "estimates" and subpath[2] == "items" and subpath[4] == "decision" and method == "POST":
        estimate = next((e for e in estimates if e["id"] == subpath[1]), None)
        line = next((x for x in estimate["items"] if x["id"] == subpath[3]), None) if estimate else None
        if estimate is None or line is None:
            raise HTTPException(status_code=404, detail="Estimate line not found")
        decision = body.get("decision")
        if decision not in {"APPROVED", "DECLINED"} or line["status"] != "PENDING":
            raise HTTPException(status_code=422, detail="Estimate line cannot take that decision")
        line["status"] = decision
        line["customer_notes"] = body.get("notes")
        estimate["approved_total"] = round(sum(x["line_total"] for x in estimate["items"] if x["status"] == "APPROVED"), 2)
        estimate["status"] = "APPROVED" if all(x["status"] != "PENDING" for x in estimate["items"]) else "PARTIALLY_APPROVED"
        return {"estimate": _copy(estimate), "approved_total": estimate["approved_total"], "remaining_to_decide": sum(x["status"] == "PENDING" for x in estimate["items"])}
    if sub == "invoices" and method == "GET":
        return [_copy(x) | {"status_view": _status_view(x["status"])} for x in invoices]
    if len(subpath) == 2 and subpath[0] == "invoices" and method == "GET":
        invoice = next((x for x in invoices if x["id"] == subpath[1]), None)
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        return _copy(invoice) | {"status_view": _status_view(invoice["status"])}
    if len(subpath) == 3 and subpath[0] == "invoices" and subpath[2] == "payments" and method == "POST":
        invoice = next((x for x in invoices if x["id"] == subpath[1]), None)
        amount = float(body.get("amount", 0))
        if invoice is None:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if amount <= 0 or amount > invoice["balance"] or invoice["status"] == "VOID":
            raise HTTPException(status_code=422, detail="Payment amount is outside the invoice balance")
        payment = _create("payments", "payment", {"invoice_id": invoice["id"], "amount": amount, "method": body.get("method", "CARD"), "status": "COMPLETED", "payment_date": _now().isoformat(), "reference": body.get("reference", "DEMO-PAYMENT"), "notes": body.get("notes"), "recorded_by_id": person["id"], "void_reason": None, "voided_at": None}, person, "payment")
        invoice["amount_paid"] = round(invoice["amount_paid"] + amount, 2)
        invoice["balance"] = round(invoice["total"] - invoice["amount_paid"], 2)
        invoice["status"] = "PAID" if invoice["balance"] == 0 else "PARTIALLY_PAID"
        return {"payment": payment, "invoice_status": invoice["status"], "invoice_status_view": _status_view(invoice["status"]), "invoice_balance": invoice["balance"]}
    if sub == "payments" and method == "GET":
        invoice_ids = {x["id"] for x in invoices}
        return [_copy(x) | {"invoice_number": next((i["invoice_number"] for i in invoices if i["id"] == x["invoice_id"]), "")} for x in STATE["payments"] if x["invoice_id"] in invoice_ids]
    raise HTTPException(status_code=404, detail="Portal resource not found")


app = FastAPI(
    title="AutoFix Demo API",
    version="1.0.0",
    description="Isolated in-memory API for demonstrating AutoFix workflows. No production database, email, or payment provider is used.",
)


@app.get("/health", tags=["Health"])
async def health() -> dict[str, str]:
    return {"status": "healthy", "app": "AutoFix Demo API", "storage": "in-memory"}


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PATCH", "PUT", "DELETE"], include_in_schema=True)
async def demo_dispatch(path: str, request: Request) -> Any:
    parts = [unquote(part) for part in path.strip("/").split("/") if part]
    method = request.method
    query = dict(request.query_params)
    body: dict[str, Any] = {}
    if method in {"POST", "PATCH", "PUT"}:
        try:
            raw = await request.json()
            if isinstance(raw, dict):
                body = raw
        except Exception:
            body = {}

    if parts and parts[0] == "auth":
        sub = "/".join(parts[1:])
        if sub == "login" and method == "POST":
            person = next((u for u in USERS if u["email"].lower() == str(body.get("email", "")).lower() and u["password"] == body.get("password")), None)
            if person is None:
                raise HTTPException(status_code=401, detail="Invalid email or password")
            from urllib.parse import quote
            token_email = quote(person["email"], safe="")
            return {"access_token": "demo-access:" + token_email, "refresh_token": "demo-refresh:" + token_email, "token_type": "bearer", "expires_in": 1800}
        if sub == "refresh" and method == "POST":
            token = str(body.get("refresh_token", ""))
            if not token.startswith("demo-refresh:"):
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            email = unquote(token[len("demo-refresh:"):])
            person = next((u for u in USERS if u["email"] == email), None)
            if person is None:
                raise HTTPException(status_code=401, detail="Invalid refresh token")
            from urllib.parse import quote
            token_email = quote(person["email"], safe="")
            return {"access_token": "demo-access:" + token_email, "refresh_token": "demo-refresh:" + token_email, "token_type": "bearer", "expires_in": 1800}
        if sub in {"register", "verification/resend", "password-reset/request", "password-reset/confirm", "verify-email"} and method == "POST":
            return {"message": "This account workflow is simulated. Email delivery and password changes are disabled in the demo."}
        person = _user(request)
        if sub == "me" and method == "GET":
            return _safe_user(person)
        if sub == "permissions" and method == "GET":
            return {"roles": person["roles"], "permissions": _permissions(person)}
        if sub == "logout" and method == "POST":
            return {"message": "Signed out"}
        raise HTTPException(status_code=404, detail="Authentication resource not found")

    person = _user(request)
    if not parts:
        return {"message": "AutoFix demo API", "docs": "/docs"}

    if parts[0] == "reports":
        _require(person, "reports:read")
        if parts[1:] == ["dashboard"]:
            return _dashboard(person)
        period = {"start_date": _day(-29), "end_date": _day(), "days": 30}
        if parts[1:] == ["revenue"]:
            invoices = STATE["invoices"]
            payments = STATE["payments"]
            total = sum(float(x["total"]) for x in invoices if x["status"] != "VOID")
            paid = sum(float(x["amount"]) for x in payments if x["status"] == "COMPLETED")
            return {"period": period, "invoiced_total": total, "invoice_count": len(invoices), "average_invoice": round(total / max(1, len(invoices)), 2), "collected_total": paid, "payment_count": len(payments), "voided_total": 0, "void_count": 0, "outstanding_balance": round(sum(float(x["balance"]) for x in invoices), 2), "outstanding_count": sum(x["balance"] > 0 for x in invoices), "overdue_balance": sum(float(x["balance"]) for x in invoices if x["is_overdue"]), "overdue_count": sum(x["is_overdue"] for x in invoices), "invoiced_by_status": {}, "series": [{"period_start": _day(-35 + i * 7), "invoiced": 180 + i * 45, "collected": 120 + i * 35} for i in range(6)]}
        if parts[1:] == ["repair-orders"]:
            orders = STATE["repair_orders"]
            return {"period": period, "status_counts": {}, "total_orders": len(orders), "opened": len(orders), "completed": sum(x["status"] == "COMPLETED" for x in orders), "delivered": sum(x["status"] == "DELIVERED" for x in orders), "cancelled": sum(x["status"] == "CANCELLED" for x in orders), "completion_rate": 50.0, "average_cycle_hours": 5.4, "series": []}
        if parts[1:] == ["technicians"]:
            return {"period": period, "technicians": [], "totals": {"actual_hours": 0.8, "billable_hours": 0.8, "labor_revenue": 88.0}}
        if parts[1:] == ["inventory-value"]:
            rows = STATE["parts"]
            return {"as_of": _now().isoformat(), "cost_value": sum(x["quantity_on_hand"] * x["unit_cost"] for x in rows), "retail_value": sum(x["quantity_on_hand"] * x["unit_price"] for x in rows), "potential_margin": sum(x["quantity_on_hand"] * (x["unit_price"] - x["unit_cost"]) for x in rows), "part_count": len(rows), "units_on_hand": sum(x["quantity_on_hand"] for x in rows), "low_stock_count": sum(0 < x["quantity_on_hand"] <= x["reorder_level"] for x in rows), "out_of_stock_count": sum(x["quantity_on_hand"] == 0 for x in rows), "by_category": [], "low_stock_items": [], "top_movers": []}
        if parts[1:] == ["customer-retention"]:
            return {"period": period, "customers_billed": 4, "new_customers": 1, "returning_customers": 3, "repeat_rate": 75.0, "settled_revenue": 570.0, "revenue_per_customer": 142.5, "top_customers": []}
        raise HTTPException(status_code=404, detail="Report not found")

    if parts[0] == "notifications":
        own = [x for x in STATE["notifications"] if x["user_id"] == person["id"]]
        if parts[1:] == ["unread-count"] and method == "GET":
            return {"unread_count": sum(not x["is_read"] for x in own)}
        if parts[1:] == ["summary"] and method == "GET":
            unread = [x for x in own if not x["is_read"]]
            return {"unread_count": len(unread), "high_priority_unread": sum(x["priority"] == "HIGH" for x in unread), "latest_unread_at": unread[0]["created_at"] if unread else None}
        if not parts[1:] and method == "GET":
            return {"notifications": _copy(own), "total": len(own), "unread_count": sum(not x["is_read"] for x in own)}
        if parts[1:] == ["mark-all-read"] and method == "POST":
            for item in own:
                item["is_read"] = True
            return {"message": "All notifications marked read"}
        if len(parts) == 3 and parts[2] in {"read", "unread"} and method == "POST":
            item = _get("notifications", parts[1])
            if item["user_id"] != person["id"]:
                raise HTTPException(status_code=404, detail="Notification not found")
            item["is_read"] = parts[2] == "read"
            return _copy(item)

    if parts[0] == "portal":
        if person["roles"][0] != "CUSTOMER":
            raise HTTPException(status_code=403, detail="Customer portal access required")
        return _portal(person, parts[1:], method, body)

    if parts[0] == "customers" and parts[1:] == ["search"] and method == "GET":
        _require(person, "customers:read")
        term = query.get("q", "").lower()
        return [_copy(x) for x in STATE["customers"] if term in (x["first_name"] + " " + x["last_name"] + " " + str(x["email"])).lower()][:int(query.get("limit", "20"))]
    if parts[0] == "vehicles" and parts[1:] == ["search"] and method == "GET":
        _require(person, "vehicles:read")
        term = query.get("q", "").lower()
        return [_copy(x) for x in STATE["vehicles"] if term in (x["make"] + " " + x["model"] + " " + str(x["license_plate"])).lower()][:int(query.get("limit", "20"))]
    if parts[0] == "appointments" and parts[1:] == ["calendar"] and method == "GET":
        _require(person, "appointments:read")
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in STATE["appointments"]:
            grouped.setdefault(item["scheduled_start"][:10], []).append(_copy(item))
        days = [{"date": key, "appointments": value} for key, value in sorted(grouped.items())]
        return {"view": query.get("view", "week"), "range_start": days[0]["date"] if days else _day(), "range_end": days[-1]["date"] if days else _day(), "total": len(STATE["appointments"]), "days": days}
    if parts[0] == "appointments" and parts[1:] == ["check-conflicts"] and method == "POST":
        _require(person, "appointments:create")
        return {"has_conflict": False, "conflicts": []}
    if parts[0] == "parts" and parts[1:] == ["categories"] and method == "GET":
        _require(person, "parts:read")
        return sorted({x["category"] for x in STATE["parts"]})
    if parts[0] == "parts" and parts[1:] == ["low-stock"] and method == "GET":
        _require(person, "parts:read")
        return [{"part_id": x["id"], "part_number": x["part_number"], "name": x["name"], "category": x["category"], "brand": x["brand"], "location": x["location"], "quantity_on_hand": x["quantity_on_hand"], "reorder_level": x["reorder_level"], "shortage": max(0, x["reorder_level"] - x["quantity_on_hand"]), "stock_status": "OUT_OF_STOCK" if x["quantity_on_hand"] == 0 else "LOW_STOCK"} for x in STATE["parts"] if x["quantity_on_hand"] <= x["reorder_level"]]
    if parts[0] == "inventory" and parts[1:] in (["stock-summary"], ["stock-levels"], ["low-stock"]):
        _require(person, "inventory:read")
        rows = STATE["parts"]
        if parts[1:] == ["stock-summary"]:
            return {"total_parts": len(rows), "total_units": sum(x["quantity_on_hand"] for x in rows), "total_stock_value": sum(x["quantity_on_hand"] * x["unit_cost"] for x in rows), "low_stock_count": sum(0 < x["quantity_on_hand"] <= x["reorder_level"] for x in rows), "out_of_stock_count": sum(x["quantity_on_hand"] == 0 for x in rows)}
        levels = [{"part_id": x["id"], "part_number": x["part_number"], "name": x["name"], "category": x["category"], "location": x["location"], "quantity_on_hand": x["quantity_on_hand"], "reorder_level": x["reorder_level"], "unit_cost": x["unit_cost"], "stock_value": x["quantity_on_hand"] * x["unit_cost"], "stock_status": "OUT_OF_STOCK" if x["quantity_on_hand"] == 0 else "LOW_STOCK" if x["quantity_on_hand"] <= x["reorder_level"] else "IN_STOCK", "is_low_stock": x["quantity_on_hand"] <= x["reorder_level"]} for x in rows]
        return levels if parts[1:] == ["stock-levels"] else [x for x in levels if x["is_low_stock"]]
    if parts[0] == "inventory" and len(parts) == 4 and parts[1] == "parts" and parts[3] == "history" and method == "GET":
        _require(person, "inventory:read")
        return [_copy(x) for x in STATE["inventory_transactions"] if x["part_id"] == parts[2]]
    if parts[0] == "purchase_orders" and parts[1:] == ["summary"] and method == "GET":
        _require(person, "purchase_orders:read")
        orders = STATE["purchase_orders"]
        return {"total_orders": len(orders), "draft_orders": sum(x["status"] == "DRAFT" for x in orders), "sent_orders": sum(x["status"] == "SENT" for x in orders), "overdue_orders": sum(x["is_overdue"] for x in orders), "total_order_value": sum(x["total_amount"] for x in orders)}
    if parts[0] == "suppliers" and parts[1:] == ["active"] and method == "GET":
        _require(person, "suppliers:read")
        return [_copy(x) for x in STATE["suppliers"] if x["is_active"] and (query.get("preferred_only") != "true" or x["is_preferred"])]
    if parts[0] == "qc" and parts[1:] == ["queue"] and method == "GET":
        _require(person, "qc:read")
        return [_copy(x) | {"repair_order": _copy(next((r for r in STATE["repair_orders"] if r["id"] == x["repair_order_id"]), None))} for x in STATE["quality_checks"]]
    if parts[0] == "labor" and parts[1:] == ["dashboard"] and method == "GET":
        _require(person, "labor:read")
        return {"technician_id": query.get("technician_id"), "open_tasks": [], "labor_today": _copy(STATE["labor"]), "hours_today": sum(x["actual_hours"] for x in STATE["labor"]), "hours_this_week": sum(x["actual_hours"] for x in STATE["labor"])}
    if parts[0] == "audit" and parts[1:] == ["actions"] and method == "GET":
        _require(person, "audit_logs:read")
        return {"actions": ["CREATED", "UPDATED", "STATUS_CHANGED", "PAYMENT_RECORDED"]}
    if parts[0] == "audit" and parts[1:] == ["entity-types"] and method == "GET":
        _require(person, "audit_logs:read")
        return {"entity_types": ["customer", "vehicle", "appointment", "estimate", "repair_order", "invoice", "payment"]}

    root = parts[0]
    collection = COLLECTIONS.get(root)
    if collection is None:
        raise HTTPException(status_code=404, detail="Demo resource not found")
    if len(parts) == 1:
        if method == "GET":
            _require(person, READ_PERMISSION[root])
            return _paged(STATE[collection], query)
        if method == "POST":
            _require(person, WRITE_PERMISSION[root])
            entity = root[:-1] if root.endswith("s") else root
            item = _create(collection, entity, body, person, entity)
            if root == "customers":
                item.setdefault("preferred_contact", "EMAIL")
                item.setdefault("customer_status", "ACTIVE")
                item.setdefault("addresses", [])
            if root == "vehicles":
                item.setdefault("status", "ACTIVE")
                item.setdefault("mileage_records", [])
            return item
        raise HTTPException(status_code=405, detail="Method not allowed")

    item_id = parts[1]
    if root == "customers" and len(parts) == 3 and parts[2] == "vehicles" and method == "GET":
        _require(person, "customers:read")
        _get("customers", item_id)
        return [_copy(x) for x in STATE["vehicles"] if x["customer_id"] == item_id]
    if root == "vehicles" and len(parts) == 3 and parts[2] == "mileage":
        _require(person, "vehicles:read" if method == "GET" else "vehicles:create")
        vehicle = _get("vehicles", item_id)
        if method == "GET":
            return _copy(vehicle.get("mileage_records", []))
        record = {"id": _new_id("mileage"), "vehicle_id": item_id, "mileage": int(body.get("mileage", vehicle.get("mileage") or 0)), "source": body.get("source", "MANUAL"), "notes": body.get("notes"), "created_at": _now().isoformat()}
        vehicle.setdefault("mileage_records", []).append(record)
        vehicle["mileage"] = record["mileage"]
        return record

    item = _get(collection, item_id)
    if len(parts) == 2:
        if method == "GET":
            _require(person, READ_PERMISSION[root])
            return _copy(item)
        if method in {"PATCH", "PUT"}:
            _require(person, WRITE_PERMISSION[root])
            item.update(_copy(body))
            item["updated_at"] = _now().isoformat()
            _audit(person, "UPDATED", root[:-1] if root.endswith("s") else root, item)
            return _copy(item)
        if method == "DELETE":
            _require(person, WRITE_PERMISSION[root])
            STATE[collection].remove(item)
            return Response(status_code=204)
        raise HTTPException(status_code=405, detail="Method not allowed")

    action = parts[2]
    if root == "appointments" and action == "status" and method == "PATCH":
        _require(person, "appointments:update")
        item["status"] = body.get("status", query.get("status", item["status"]))
        item["cancellation_reason"] = body.get("cancellation_reason")
        item["updated_at"] = _now().isoformat()
        return _copy(item)
    if root == "check_ins" and action == "status" and method == "PATCH":
        _require(person, "check_ins:update")
        item["status"] = body.get("status", query.get("status", item["status"]))
        item["updated_at"] = _now().isoformat()
        return _copy(item)
    if root == "inspections":
        _require(person, "inspections:update")
        if action == "status" and method == "PATCH":
            item["status"] = body.get("status", query.get("status", item["status"]))
            item["updated_at"] = _now().isoformat()
            return _copy(item)
        if action == "report" and method == "GET":
            return {"inspection": _copy(item), "summary": "Inspection report generated by the demo API"}
        if action == "items" and len(parts) == 3 and method == "POST":
            new_item = _copy(body)
            new_item["id"] = _new_id("inspection-item")
            new_item.setdefault("photos", [])
            item.setdefault("items", []).append(new_item)
            return _copy(new_item)
        if action == "items" and len(parts) == 5:
            target = next((x for x in item.get("items", []) if x["id"] == parts[3]), None)
            if target is None:
                raise HTTPException(status_code=404, detail="Inspection item not found")
            if parts[4] == "unused":
                raise HTTPException(status_code=404, detail="Inspection action not found")
            if method == "PATCH":
                target.update(_copy(body))
                return _copy(target)
            if method == "DELETE":
                item["items"].remove(target)
                return Response(status_code=204)
    if root == "estimates":
        _require(person, "estimates:update")
        if action == "summary" and method == "GET":
            pending = sum(x["status"] == "PENDING" for x in item.get("items", []))
            approved = sum(x["status"] == "APPROVED" for x in item.get("items", []))
            declined = sum(x["status"] == "DECLINED" for x in item.get("items", []))
            return {"estimate_id": item["id"], "estimate_number": item["estimate_number"], "status": item["status"], "is_expired": item.get("is_expired", False), "can_decide": item["status"] == "SENT", "valid_until": item.get("valid_until"), "totals": {"subtotal": item["subtotal"], "discount_amount": item["discount_amount"], "tax_rate": item["tax_rate"], "tax_amount": item["tax_amount"], "total": item["total"], "approved_total": item["approved_total"]}, "counts": {"total": len(item.get("items", [])), "pending": pending, "approved": approved, "declined": declined}}
        if action == "send" and method == "POST":
            item["status"] = "SENT"
            item["sent_at"] = _now().isoformat()
            return _copy(item)
        if action == "status" and method == "PATCH":
            item["status"] = query.get("status", body.get("status", item["status"]))
            return _copy(item)
        if action == "items" and len(parts) == 3 and method == "POST":
            line = _copy(body)
            line["id"] = _new_id("estimate-item")
            line.setdefault("status", "PENDING")
            line.setdefault("quantity", 1)
            line.setdefault("discount_amount", 0)
            line["line_total"] = round(float(line.get("quantity", 1)) * float(line.get("unit_price", 0)) - float(line.get("discount_amount", 0)), 2)
            item.setdefault("items", []).append(line)
            return _copy(line)
        if action == "items" and len(parts) == 5:
            line = next((x for x in item.get("items", []) if x["id"] == parts[3]), None)
            if line is None:
                raise HTTPException(status_code=404, detail="Estimate line not found")
            if parts[4] == "decision" and method == "POST":
                decision = body.get("decision")
                if decision not in {"APPROVED", "DECLINED"}:
                    raise HTTPException(status_code=422, detail="Choose APPROVED or DECLINED")
                line["status"] = decision
                line["customer_notes"] = body.get("notes")
                item["approved_total"] = round(sum(x["line_total"] for x in item["items"] if x["status"] == "APPROVED"), 2)
                item["status"] = "APPROVED" if all(x["status"] != "PENDING" for x in item["items"]) else "PARTIALLY_APPROVED"
                return _copy(item)
            if method == "PATCH":
                line.update(_copy(body))
                return _copy(line)
            if method == "DELETE":
                item["items"].remove(line)
                return Response(status_code=204)
    if root == "repair_orders":
        _require(person, "repair_orders:update")
        if action == "summary" and method == "GET":
            tasks = item.get("tasks", [])
            return {"repair_order_id": item["id"], "ro_number": item["ro_number"], "status": item["status"], "is_terminal": item["status"] in {"DELIVERED", "CANCELLED"}, "tasks_editable": item["status"] not in {"DELIVERED", "CANCELLED"}, "all_tasks_done": all(t["status"] in {"COMPLETED", "SKIPPED"} for t in tasks), "counts": {"total": len(tasks), "pending": sum(t["status"] == "PENDING" for t in tasks), "in_progress": sum(t["status"] == "IN_PROGRESS" for t in tasks), "completed": sum(t["status"] == "COMPLETED" for t in tasks), "skipped": sum(t["status"] == "SKIPPED" for t in tasks)}}
        if action == "status" and method == "PATCH":
            item["status"] = query.get("status", body.get("status", item["status"]))
            item["updated_at"] = _now().isoformat()
            return _copy(item)
        if action == "tasks" and len(parts) == 3 and method == "POST":
            task = _copy(body) | {"id": _new_id("task"), "repair_order_id": item["id"], "status": "PENDING", "sequence": len(item.get("tasks", [])) + 1, "completed_at": None}
            item.setdefault("tasks", []).append(task)
            return _copy(task)
        if action == "tasks" and len(parts) == 5:
            task = next((x for x in item.get("tasks", []) if x["id"] == parts[3]), None)
            if task is None:
                raise HTTPException(status_code=404, detail="Repair task not found")
            if parts[4] == "status" and method == "PATCH":
                task["status"] = query.get("status", body.get("status", task["status"]))
                task["completed_at"] = _now().isoformat() if task["status"] == "COMPLETED" else None
                item["all_tasks_done"] = all(x["status"] in {"COMPLETED", "SKIPPED"} for x in item["tasks"])
                return _copy(task)
            if method == "PATCH":
                task.update(_copy(body))
                return _copy(task)
            if method == "DELETE":
                item["tasks"].remove(task)
                return Response(status_code=204)
    if root == "part_requests":
        _require(person, "part_requests:update")
        if action == "decision" and method == "POST":
            decision = body.get("decision")
            if decision not in {"APPROVED", "REJECTED"}:
                raise HTTPException(status_code=422, detail="Choose APPROVED or REJECTED")
            item["status"] = decision
            item["decision_reason"] = body.get("decision_reason")
            item["approved_by_id"] = person["id"]
            return _copy(item)
        if action == "fulfil" and method == "POST":
            if item["status"] != "APPROVED":
                raise HTTPException(status_code=409, detail="Only an approved request can be fulfilled")
            item["status"] = "FULFILLED"
            item["fulfilled_by_id"] = person["id"]
            return _copy(item)
    if root == "qc":
        _require(person, "qc:update")
        if action == "pass" and method == "POST":
            item["status"], item["passed"], item["is_terminal"] = "PASSED", True, True
            item["completed_at"] = _now().isoformat()
            return _copy(item)
        if action == "fail" and method == "POST":
            item["status"], item["passed"], item["is_terminal"] = "FAILED", False, True
            item["failure_reason"] = body.get("reason")
            item["completed_at"] = _now().isoformat()
            return _copy(item)
        if action == "reverify" and method == "POST":
            item["status"], item["passed"], item["is_terminal"] = "IN_PROGRESS", False, False
            item["attempt_number"] += 1
            return _copy(item)
        if action == "checks" and len(parts) == 4 and method == "PATCH":
            check = next((x for x in item.get("checks", []) if x["check_type"] == parts[3]), None)
            if check is None:
                raise HTTPException(status_code=404, detail="QC check not found")
            check.update(_copy(body))
            check["manually_overridden"] = True
            return _copy(item)
    if root == "invoices":
        _require(person, "invoices:create")
        if action == "summary" and method == "GET":
            return {"invoice_id": item["id"], "invoice_number": item["invoice_number"], "status": item["status"], "total": item["total"], "amount_paid": item["amount_paid"], "balance": item["balance"], "is_overdue": item["is_overdue"]}
        if action == "issue" and method == "POST":
            item["status"], item["issued_at"] = "ISSUED", _now().isoformat()
            return _copy(item)
        if action == "void" and method == "POST":
            item["status"], item["void_reason"], item["voided_at"] = "VOID", query.get("reason") or body.get("reason"), _now().isoformat()
            return _copy(item)
        if action == "items" and len(parts) == 3 and method == "POST":
            line = _copy(body) | {"id": _new_id("invoice-item")}
            item.setdefault("items", []).append(line)
            return _copy(line)
        if action == "items" and len(parts) == 5:
            line = next((x for x in item.get("items", []) if x["id"] == parts[3]), None)
            if line is None:
                raise HTTPException(status_code=404, detail="Invoice item not found")
            if method == "PATCH":
                line.update(_copy(body))
                return _copy(line)
            if method == "DELETE":
                item["items"].remove(line)
                return Response(status_code=204)
    if root == "payments":
        _require(person, "payments:create")
        if action == "void" and method == "POST":
            item["status"], item["void_reason"], item["voided_at"] = "VOIDED", body.get("reason"), _now().isoformat()
            invoice = next((x for x in STATE["invoices"] if x["id"] == item["invoice_id"]), None)
            if invoice:
                invoice["amount_paid"] = max(0, invoice["amount_paid"] - item["amount"])
                invoice["balance"] = round(invoice["total"] - invoice["amount_paid"], 2)
                invoice["status"] = "ISSUED" if invoice["amount_paid"] == 0 else "PARTIALLY_PAID"
            return _copy(item)
    if root == "purchase_orders":
        _require(person, "purchase_orders:update")
        if action == "by-number" and method == "GET":
            found = next((x for x in STATE["purchase_orders"] if x["po_number"] == item_id), None)
            if found is None:
                raise HTTPException(status_code=404, detail="Purchase order not found")
            return _copy(found)
        if action == "send" and method == "POST":
            item["status"], item["sent_at"] = "SENT", _now().isoformat()
            return _copy(item)
        if action == "cancel" and method == "POST":
            item["status"], item["cancel_reason"], item["cancelled_at"] = "CANCELLED", body.get("reason"), _now().isoformat()
            return _copy(item)
        if action == "receive" and method == "POST":
            for line in body.get("items", []):
                po_line = next((x for x in item.get("items", []) if x["id"] == line.get("item_id")), None)
                if po_line is None:
                    raise HTTPException(status_code=404, detail="Purchase order line not found")
                quantity = int(line.get("quantity", 0))
                if quantity < 0 or quantity > po_line["quantity_ordered"] - po_line["quantity_received"]:
                    raise HTTPException(status_code=422, detail="Received quantity exceeds remaining order quantity")
                po_line["quantity_received"] += quantity
                part = next((x for x in STATE["parts"] if x["id"] == po_line["part_id"]), None)
                if part:
                    before = part["quantity_on_hand"]
                    part["quantity_on_hand"] += quantity
                    STATE["inventory_transactions"].append({"id": _new_id("txn"), "part_id": part["id"], "transaction_type": "RECEIPT", "quantity": quantity, "quantity_before": before, "quantity_after": part["quantity_on_hand"], "unit_cost": line.get("unit_cost", po_line["unit_cost"]), "reference": item["po_number"], "notes": body.get("notes"), "created_at": _now().isoformat()})
            item["total_units_received"] = sum(x["quantity_received"] for x in item.get("items", []))
            item["is_fully_received"] = all(x["quantity_received"] >= x["quantity_ordered"] for x in item.get("items", []))
            item["status"] = "RECEIVED" if item["is_fully_received"] else "PARTIALLY_RECEIVED"
            item["received_at"] = _now().isoformat()
            return {"purchase_order": _copy(item), "inventory_transactions": []}
        if action == "items" and len(parts) == 3 and method == "POST":
            line = _copy(body) | {"id": _new_id("po-item"), "quantity_received": 0}
            line["line_total"] = float(line.get("quantity_ordered", 1)) * float(line.get("unit_cost", 0))
            item.setdefault("items", []).append(line)
            item["item_count"] = len(item["items"])
            return _copy(item)
        if action == "items" and len(parts) == 5:
            line = next((x for x in item.get("items", []) if x["id"] == parts[3]), None)
            if line is None:
                raise HTTPException(status_code=404, detail="Purchase order line not found")
            if method == "PATCH":
                line.update(_copy(body))
                return _copy(item)
            if method == "DELETE":
                item["items"].remove(line)
                return Response(status_code=204)

    raise HTTPException(status_code=404, detail="Demo action not found")
