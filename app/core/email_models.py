"""Durable SMTP outbox records without persisted verification/reset tokens."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TimestampedBase


class EmailOutbox(TimestampedBase):
    """A durable email delivery job; sensitive links are generated only in worker memory."""

    __tablename__ = "email_outbox"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_email_outbox_user_id_users"),
        nullable=False,
    )
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default=text("'PENDING'"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "message_type IN ('VERIFY_EMAIL', 'PASSWORD_RESET')",
            name="ck_email_outbox_message_type",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')",
            name="ck_email_outbox_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_email_outbox_attempts"),
        Index("ix_email_outbox_ready", "status", "available_at"),
        Index("ix_email_outbox_locked", "status", "locked_at"),
        Index("ix_email_outbox_user_id", "user_id"),
    )
