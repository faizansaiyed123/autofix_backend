"""Audit logging domain.

An audit entry answers one question: **who did this, and what did the record look
like when they did it?** Everything else about this module follows from taking
that question seriously.

The actor is stored **twice**. ``actor_id`` is a foreign key so the log can be
filtered by person, and it is ``SET NULL`` on delete because deleting a user must
not delete the evidence that they once existed. But a nulled foreign key answers
"who" with a shrug, so ``actor_email`` and ``actor_role`` are copied onto the row
as well: the log has to still name somebody after the account is gone, which is
precisely the case anybody reads an audit log for.

The entry is **append-only**. There is no update and no delete through the API,
and the absence is deliberate: an endpoint that can erase audit rows is an
endpoint for erasing evidence, and no role in this system holds one. Pruning is
an operator decision made at the database, in the open, not a feature.

``action`` is constrained, ``entity_type`` deliberately is not. The vocabulary of
things a person can *do* — create, approve, void, take money — is stable and is
worth protecting with a ``CHECK`` constraint so a typo cannot invent a new kind of
event. The vocabulary of things the shop *has* is not: auditing a new entity must
never require a migration to widen a constraint, because the day auditing a new
module needs a deploy is the day somebody ships the module without it.
"""

from app.audit.decorator import audit
from app.audit.models import (
    AUDIT_ACTION_VALUES,
    AuditAction,
    AuditLog,
)
from app.audit.registry import register_standard_entities
from app.audit.services import AuditService, record_auth_event

# Imported for its side effect: the entity registry has to be populated before
# any service tries to snapshot a row, and doing it here means every entry point
# into the package gets it without having to remember.
register_standard_entities()

__all__ = [
    "AUDIT_ACTION_VALUES",
    "AuditAction",
    "AuditLog",
    "AuditService",
    "audit",
    "record_auth_event",
    "register_standard_entities",
]
