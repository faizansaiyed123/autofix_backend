"""Pydantic schemas for the audit log.

The read schema is deliberately wide. An audit view that hides a column to keep
the payload tidy is hiding the thing somebody came to read, and unlike most
"tidiness", that cannot be undone by clicking something else.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.audit.models import AuditLog
from app.common.schemas import BaseSchema, PaginatedResponse


class AuditLogRead(BaseSchema):
    """One audit entry as the client sees it."""

    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID | None = None

    # Who. `actor_email` is the snapshot, so it survives the account being
    # deleted; `actor_id` is the live link, so it goes null at exactly that
    # moment. Both are returned because "what happened" needs the former and
    # "everything this person did" needs the latter.
    actor_id: uuid.UUID | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    actor_label: str

    entity_label: str | None = None
    summary: str | None = None
    changes: dict[str, Any] | None = None
    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None

    ip_address: str | None = None
    user_agent: str | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, entry: AuditLog) -> AuditLogRead:
        """Build from the model, taking ``actor_label`` from the property.

        Handled explicitly rather than through ``from_attributes`` alone because
        the fallback chain inside that property is the whole reason a deleted
        actor still reads as somebody.
        """
        return cls(
            id=entry.id,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            actor_id=entry.actor_id,
            actor_email=entry.actor_email,
            actor_role=entry.actor_role,
            actor_label=entry.actor_label,
            entity_label=entry.entity_label,
            summary=entry.summary,
            changes=entry.changes,
            before_state=entry.before_state,
            after_state=entry.after_state,
            ip_address=entry.ip_address,
            user_agent=entry.user_agent,
            created_at=entry.created_at,
        )


class AuditLogSummary(BaseSchema):
    """A trimmed entry, for lists where the full states would be enormous.

    ``before_state`` and ``after_state`` are omitted on purpose: a list of two
    hundred entries with two full row snapshots each is a multi-megabyte
    response that nobody asked for, and the diff plus the link to the detail
    endpoint is what the list actually renders.
    """

    id: uuid.UUID
    action: str
    entity_type: str
    entity_id: uuid.UUID | None = None
    entity_label: str | None = None
    actor_id: uuid.UUID | None = None
    actor_email: str | None = None
    actor_role: str | None = None
    actor_label: str
    summary: str | None = None
    changes: dict[str, Any] | None = None
    created_at: datetime

    @classmethod
    def from_row(cls, entry: AuditLog) -> AuditLogSummary:
        return cls(
            id=entry.id,
            action=entry.action,
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            entity_label=entry.entity_label,
            actor_id=entry.actor_id,
            actor_email=entry.actor_email,
            actor_role=entry.actor_role,
            actor_label=entry.actor_label,
            summary=entry.summary,
            changes=entry.changes,
            created_at=entry.created_at,
        )


class AuditEntityHistory(BaseSchema):
    """Everything that happened to one record."""

    entity_type: str
    entity_id: uuid.UUID
    entity_label: str | None = None
    total: int = 0
    entries: list[AuditLogSummary] = Field(default_factory=list)


AuditLogList = PaginatedResponse[AuditLogSummary]


__all__ = [
    "AuditEntityHistory",
    "AuditLogList",
    "AuditLogRead",
    "AuditLogSummary",
]
