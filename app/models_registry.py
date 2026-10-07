"""Central import point for every SQLAlchemy model in the project.

``Base.metadata`` is only populated for model modules that have actually been
imported. Anything that needs the *complete* schema — Alembic autogenerate,
``create_all`` in the test fixtures, migration parity checks — must import
this module rather than remembering to list each domain by hand.

When a new domain module is added, register it here.
"""

from __future__ import annotations

from app.audit import models as audit_models
from app.auth import models as auth_models

__all__ = [
    "audit_models",
    "auth_models",
]
