"""Add account verification, one-time token state and shared auth limits."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d9a7c3f2b611"
down_revision: str | None = "46724f21fd5a"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "email_verified", sa.Boolean(), server_default=sa.text("true"), nullable=False
    ))
    op.add_column("users", sa.Column("email_verification_token_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("email_verification_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("password_reset_token_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("password_reset_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("refresh_token_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column(
        "auth_version", sa.Integer(), server_default=sa.text("0"), nullable=False
    ))
    op.create_index("ix_users_email_verification_token_hash", "users", ["email_verification_token_hash"], unique=True)
    op.create_index("ix_users_password_reset_token_hash", "users", ["password_reset_token_hash"], unique=True)

    op.create_table(
        "auth_rate_limits",
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_count >= 1", name="ck_auth_rate_limits_attempt_count"),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index("ix_auth_rate_limits_last_seen_at", "auth_rate_limits", ["last_seen_at"], unique=False)

    op.create_table(
        "email_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("recipient_email", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=100), nullable=True),
        sa.CheckConstraint("status IN ('PENDING', 'PROCESSING', 'SENT', 'FAILED')", name="ck_email_outbox_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_email_outbox_attempts"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_outbox_ready", "email_outbox", ["status", "available_at"], unique=False)
    op.create_index("ix_email_outbox_locked", "email_outbox", ["status", "locked_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_email_outbox_locked", table_name="email_outbox")
    op.drop_index("ix_email_outbox_ready", table_name="email_outbox")
    op.drop_table("email_outbox")
    op.drop_index("ix_auth_rate_limits_last_seen_at", table_name="auth_rate_limits")
    op.drop_table("auth_rate_limits")
    op.drop_index("ix_users_password_reset_token_hash", table_name="users")
    op.drop_index("ix_users_email_verification_token_hash", table_name="users")
    op.drop_column("users", "auth_version")
    op.drop_column("users", "refresh_token_hash")
    op.drop_column("users", "password_reset_expires_at")
    op.drop_column("users", "password_reset_token_hash")
    op.drop_column("users", "email_verification_expires_at")
    op.drop_column("users", "email_verification_token_hash")
    op.drop_column("users", "email_verified")
