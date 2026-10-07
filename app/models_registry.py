"""Central import point for every SQLAlchemy model in the project.

``Base.metadata`` is only populated for model modules that have actually been
imported. Anything that needs the *complete* schema — Alembic autogenerate,
``create_all`` in the test fixtures, migration parity checks — must import
this module rather than remembering to list each domain by hand.

When a new domain module is added, register it here.
"""

from __future__ import annotations

from app.appointments import models as appointment_models
from app.audit import models as audit_models
from app.auth import models as auth_models
from app.checkins import models as checkin_models
from app.customers import models as customer_models
from app.estimates import models as estimate_models
from app.inspections import models as inspection_models
from app.inventory import models as inventory_models
from app.invoices import models as invoice_models
from app.labor import models as labor_models
from app.notifications import models as notification_models
from app.part_requests import models as part_request_models
from app.parts import models as part_models
from app.purchase_orders import models as purchase_order_models
from app.qc import models as qc_models
from app.repair_orders import models as repair_order_models
from app.service_requests import models as service_request_models
from app.suppliers import models as supplier_models
from app.vehicles import models as vehicle_models

__all__ = [
    "appointment_models",
    "audit_models",
    "auth_models",
    "checkin_models",
    "customer_models",
    "estimate_models",
    "inspection_models",
    "inventory_models",
    "invoice_models",
    "labor_models",
    "notification_models",
    "part_models",
    "part_request_models",
    "purchase_order_models",
    "qc_models",
    "repair_order_models",
    "service_request_models",
    "supplier_models",
    "vehicle_models",
]
