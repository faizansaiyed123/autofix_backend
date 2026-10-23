"""Store only email job metadata, not live verification/reset links."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f8c4d6207b91"
down_revision: str | None = "d9a7c3f2b611"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("email_outbox", sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("email_outbox", sa.Column("message_type", sa.String(length=30), nullable=True))

    op.execute("""
        UPDATE email_outbox AS outbox
        SET user_id = users.id,
            message_type = CASE
                WHEN outbox.subject ILIKE 'Verify%' THEN 'VERIFY_EMAIL'
                ELSE 'PASSWORD_RESET'
            END
        FROM users
        WHERE lower(users.email) = lower(outbox.recipient_email)
    """)
    op.execute("DELETE FROM email_outbox WHERE user_id IS NULL")

    op.alter_column("email_outbox", "user_id", nullable=False)
    op.alter_column("email_outbox", "message_type", nullable=False)
    op.create_foreign_key(
        "fk_email_outbox_user_id_users",
        "email_outbox",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_email_outbox_message_type",
        "email_outbox",
        "message_type IN ('VERIFY_EMAIL', 'PASSWORD_RESET')",
    )
    op.create_index("ix_email_outbox_user_id", "email_outbox", ["user_id"], unique=False)

    op.drop_column("email_outbox", "html_body")
    op.drop_column("email_outbox", "text_body")
    op.drop_column("email_outbox", "subject")
    op.drop_column("email_outbox", "recipient_email")


def downgrade() -> None:
    op.add_column(
        "email_outbox",
        sa.Column("recipient_email", sa.String(length=255), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "email_outbox",
        sa.Column("subject", sa.String(length=255), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "email_outbox",
        sa.Column("text_body", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.add_column(
        "email_outbox",
        sa.Column("html_body", sa.Text(), server_default=sa.text("''"), nullable=False),
    )
    op.execute("""
        UPDATE email_outbox AS outbox
        SET recipient_email = users.email
        FROM users
        WHERE users.id = outbox.user_id
    """)
    op.drop_index("ix_email_outbox_user_id", table_name="email_outbox")
    op.drop_constraint("ck_email_outbox_message_type", "email_outbox", type_="check")
    op.drop_constraint("fk_email_outbox_user_id_users", "email_outbox", type_="foreignkey")
    op.drop_column("email_outbox", "message_type")
    op.drop_column("email_outbox", "user_id")
