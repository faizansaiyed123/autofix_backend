"""Audit log data model.

One row is one thing somebody did to one record. See the package docstring for
why the actor is stored twice and why ``entity_type`` is unconstrained while
``action`` is not.
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TimestampedBase


class AuditAction(str, enum.Enum):
    """What a person did.

    A short, fixed vocabulary of *kinds of act*, deliberately kept separate from
    the entity they were performed on. ``ISSUE`` on an invoice and ``ISSUE`` on an
    estimate are the same decision about different things, and a report that
    asked "who issued what" should not have to know both module names to answer
    it.
    """

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"

    # A lifecycle move: the record did not change shape, it changed state.
    STATUS_CHANGE = "STATUS_CHANGE"

    APPROVE = "APPROVE"
    REJECT = "REJECT"

    # Documents sent to somebody.
    SEND = "SEND"
    ISSUE = "ISSUE"
    DECIDE = "DECIDE"

    # Money.
    RECORD_PAYMENT = "RECORD_PAYMENT"
    VOID_PAYMENT = "VOID_PAYMENT"
    REFUND = "REFUND"

    # Access.
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    PERMISSION_CHANGE = "PERMISSION_CHANGE"

    # Reading something sensitive enough to be worth a line of its own.
    EXPORT = "EXPORT"


AUDIT_ACTION_VALUES: tuple[str, ...] = tuple(a.value for a in AuditAction)


class AuditLog(TimestampedBase):
    """One immutable record of one action by one actor."""

    __tablename__ = "audit_logs"

    # SET NULL, never CASCADE: a user who leaves the company does not take the
    # record of what they did while they were here with them.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Snapshots. The foreign key above goes null when the account is deleted; these
    # are what the log still reads years later.
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(100), nullable=True)

    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    # Free text by design: see the package docstring.
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # The human-facing reference — an invoice number, an RO number, a customer's
    # name. An id answers "which row"; this answers "which one" to somebody
    # reading a log six months later with no database open.
    entity_label: Mapped[str | None] = mapped_column(String(200), nullable=True)

    summary: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # The point of the whole table. `changes` is the readable diff
    # ({"status": {"from": "DRAFT", "to": "ISSUED"}}); the two state columns hold
    # the full rows for the cases where a diff cannot be trusted, such as a
    # deletion where there is no "after" at all.
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    before_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    after_state: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)

    __table_args__ = (
        CheckConstraint("action IN " + str(AUDIT_ACTION_VALUES), name="ck_audit_logs_action"),
        # "Who did what to this record" is the query the whole table exists for,
        # and answering it from an unindexed pair is the difference between a
        # usable history and a timeout.
        Index("ix_audit_logs_entity", "entity_type", "entity_id"),
        # The admin view's default ordering: newest first, across every actor.
        Index("ix_audit_logs_recent", "created_at", "action"),
    )

    @property
    def actor_label(self) -> str:
        """How to name the actor in a list.

        Falls back through the snapshots so a deleted account still reads as
        somebody rather than as a null.
        """
        if self.actor_email:
            return self.actor_email
        if self.actor_id:
            return str(self.actor_id)
        return "anonymous"

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} {self.entity_type} by {self.actor_label}>"


__all__ = [
    "AUDIT_ACTION_VALUES",
    "AuditAction",
    "AuditLog",
]
