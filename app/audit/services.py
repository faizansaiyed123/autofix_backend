"""Audit business logic: writing entries, and reading them back.

Two rules shape everything in this file.

**An audit entry is part of the operation it describes.** :meth:`AuditService.record`
flushes into the caller's transaction by default and never commits on its own,
so an entry and the change it describes either both land or neither does. The
one thing it will not do is swallow a failure: an audit write that fails loudly
is a bug, an audit write that fails quietly is a lie, and the shop is choosing
to find out.

**The log is append-only.** There is no update method and no delete method, and
their absence is the feature — see the package docstring. Reading is the only
thing this service offers besides writing.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AuditAction, AuditLog
from app.audit.registry import (
    diff_states,
    get_spec,
    label_for,
    load_state,
    snapshot,
)
from app.common.exceptions import NotFoundError

logger = logging.getLogger("autofix.audit.services")

# Fields that change on every write and would drown a diff in noise. `updated_at`
# in particular changes on every single commit, so including it would put a
# meaningless entry in every single audit row.
NOISE_FIELDS: frozenset[str] = frozenset({"updated_at", "last_login_at"})


class AuditService:
    """Writes audit entries and reads the log."""

    def __init__(self, db: AsyncSession):
        self.db = db

    # --- writing ------------------------------------------------------------

    async def record(
        self,
        *,
        action: AuditAction | str,
        entity_type: str,
        actor: Any = None,
        entity_id: Any = None,
        entity_label: str | None = None,
        summary: str | None = None,
        changes: dict | None = None,
        before_state: dict | None = None,
        after_state: dict | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        commit: bool = False,
    ) -> AuditLog:
        """Append one entry and return it.

        Flushes rather than commits by default so the caller can land the entry
        in the same transaction as the change it describes. A rolled-back
        estimate must not leave behind an audit row claiming it was approved.
        """
        action_value = action.value if isinstance(action, AuditAction) else str(action)

        spec = get_spec(entity_type)
        if entity_label is None:
            entity_label = label_for(after_state or before_state, spec)

        entry = AuditLog(
            actor_id=getattr(actor, "id", None),
            actor_email=getattr(actor, "email", None),
            actor_role=await self._actor_role(actor),
            action=action_value,
            entity_type=entity_type,
            entity_id=_as_uuid(entity_id),
            entity_label=entity_label,
            summary=_clip(summary, 300),
            changes=_clip_changes(changes),
            before_state=before_state,
            after_state=after_state,
            ip_address=_clip(ip_address, 45),
            user_agent=_clip(user_agent, 300),
        )
        self.db.add(entry)
        if commit:
            await self.db.commit()
        else:
            await self.db.flush()
        return entry

    async def record_change(
        self,
        *,
        action: AuditAction | str,
        entity_type: str,
        entity_id: Any = None,
        actor: Any = None,
        before_state: dict | None = None,
        after_state: dict | None = None,
        summary: str | None = None,
        **kwargs: Any,
    ) -> AuditLog:
        """Record an entry with the diff computed from the two snapshots.

        Thin wrapper over :meth:`record` that exists so callers never have to
        build the ``changes`` dict by hand — a hand-built diff that forgets one
        field is worse than no diff, because it looks complete.

        Both snapshots are supplied by the caller because only the caller knows
        *when* to take them: the "before" has to be read before the mutation
        runs, and the "after" after it commits.
        """
        spec = get_spec(entity_type)
        if _is_delete(action) and after_state is None:
            # A hard delete has no "after", so a mechanical diff would report
            # every column as having become null — technically true and entirely
            # useless. The full prior row is in ``before_state``, which is what
            # somebody reading this entry six months later actually wants.
            changes: dict | None = {}
        else:
            changes = diff_states(
                before_state,
                after_state,
                ignore=NOISE_FIELDS | set(spec.sensitive_fields if spec else ()),
            )
        return await self.record(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            actor=actor,
            summary=summary,
            changes=changes,
            before_state=before_state,
            after_state=after_state,
            **kwargs,
        )

    async def snapshot_entity(self, entity_type: str, entity_id: Any) -> dict | None:
        """Read a row as it stands right now, for use as a ``before`` snapshot.

        Called *before* the mutation the entry describes. A route that audits a
        delete has no other chance to see what it deleted.
        """
        return await load_state(self.db, entity_type, entity_id)

    async def snapshot_row(self, entity_type: str, row: Any) -> dict:
        """Snapshot an in-memory model instance (usually one just created)."""
        spec = get_spec(entity_type)
        return snapshot(spec, row) if spec is not None else {}

    # --- reading ------------------------------------------------------------

    async def get(self, log_id: uuid.UUID | str) -> AuditLog:
        result = await self.db.execute(
            select(AuditLog).where(AuditLog.id == str(log_id))
        )
        entry = result.scalar_one_or_none()
        if entry is None:
            raise NotFoundError(f"Audit log {log_id} not found")
        return entry

    async def list_logs(
        self,
        *,
        actor_id: uuid.UUID | str | None = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        page: int = 1,
        size: int = 50,
    ) -> tuple[list[AuditLog], int]:
        """The log, newest first, with filters and pagination.

        Newest first because that is the question being asked ninety-nine times
        out of a hundred: *what just happened*.
        """
        conditions = self._conditions(
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            date_from=date_from,
            date_to=date_to,
        )
        total = await self._count(conditions)
        result = await self.db.execute(
            select(AuditLog)
            .where(*conditions)
            .order_by(AuditLog.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(result.scalars().all()), total

    async def entity_history(
        self,
        entity_type: str,
        entity_id: uuid.UUID | str,
        *,
        limit: int = 50,
    ) -> list[AuditLog]:
        """Everything that has happened to one record, newest first.

        The "show me the history of this RO" view. Separate from the general
        listing because it is scoped by identity, not by filter, and because it
        has no filters at all — asking about one record and getting every other
        record mixed in would be a bug, not a wide view.
        """
        result = await self.db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == entity_type,
                AuditLog.entity_id == str(entity_id),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_logs(self, **filters: Any) -> int:
        return await self._count(self._conditions(**filters))

    # --- internals ----------------------------------------------------------

    def _conditions(
        self,
        *,
        actor_id: Any = None,
        action: str | None = None,
        entity_type: str | None = None,
        entity_id: Any = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[Any]:
        """Build the WHERE clauses once so the count and the page cannot disagree.

        Two separate implementations of "the same filter" is how a list endpoint
        ends up reporting 400 rows over 12 pages.
        """
        conditions: list[Any] = []
        if actor_id is not None:
            conditions.append(AuditLog.actor_id == str(actor_id))
        if action:
            # An unknown action is not an error; it simply matches nothing. The
            # filter is free text typed by an operator, and a log view that 400s
            # on a typo is worse than one that shows an empty page.
            conditions.append(AuditLog.action == action)
        if entity_type:
            conditions.append(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            conditions.append(AuditLog.entity_id == str(entity_id))
        if date_from is not None:
            conditions.append(AuditLog.created_at >= _start_of_day(date_from))
        if date_to is not None:
            # `<=` against midnight of the last day would silently drop every
            # entry written during that day, which is most of them.
            conditions.append(AuditLog.created_at < _start_of_day(date_to) + timedelta(days=1))
        return conditions

    async def _count(self, conditions: list[Any]) -> int:
        stmt = select(func.count(AuditLog.id))
        if conditions:
            stmt = stmt.where(*conditions)
        return int((await self.db.execute(stmt)).scalar_one() or 0)

    async def _actor_role(self, actor: Any) -> str | None:
        """The actor's primary role, snapshotted at the time of the action.

        Read through the user's role rows rather than stored on ``users`` so the
        answer matches what the permission check actually consulted. A deleted
        actor reads ``None`` here, which is why the email is stored beside it.
        """
        if actor is None or getattr(actor, "id", None) is None:
            return None
        # Imported lazily: app.auth.services imports this module for login
        # auditing, and a module-level import here would close the cycle.
        from app.auth.models import Role, UserRole
        from app.auth.permissions import RoleEnum

        result = await self.db.execute(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                UserRole.user_id == actor.id,
                Role.name == RoleEnum.OWNER.value,
            )
        )
        if result.scalar_one_or_none():
            return RoleEnum.OWNER.value
        result = await self.db.execute(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == actor.id)
            .order_by(Role.name)
            .limit(1)
        )
        return result.scalar_one_or_none()


def _is_delete(action: AuditAction | str) -> bool:
    value = action.value if isinstance(action, AuditAction) else str(action)
    return value == AuditAction.DELETE.value


def _as_uuid(value: Any) -> uuid.UUID | None:
    """Coerce an id to a UUID, or ``None``.

    ``entity_id`` is nullable on purpose: a login attempt against an address
    that does not exist has no record to point at, and refusing to log it would
    mean the only interesting failures are the ones nobody sees.
    """
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _clip(value: str | None, limit: int) -> str | None:
    """Truncate to the column width rather than failing the write.

    A long user agent is a nuisance, not a reason to lose the audit entry.
    """
    if value is None:
        return None
    return value[:limit]


def _clip_changes(changes: dict | None) -> dict | None:
    """Keep a diff small enough to be read.

    A whole-row overwrite can legitimately change twenty fields; an audit log
    exists to be read, so the diff is capped and the cap is recorded rather than
    applied silently. The full states are still in ``before_state``/``after_state``
    for anyone who needs every field.
    """
    if not changes:
        return changes
    limit = 25
    if len(changes) <= limit:
        return changes
    kept = dict(list(changes.items())[:limit])
    kept["_truncated"] = {
        "from": None,
        "to": f"{len(changes) - limit} further field(s) not listed; see before_state/after_state",
    }
    return kept


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, datetime.min.time(), tzinfo=UTC)


async def record_auth_event(
    db: AsyncSession,
    *,
    action: AuditAction,
    user: Any = None,
    attempted_email: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog | None:
    """Write a login, logout or failed-login entry.

    A module function rather than a service method because the auth routes have
    no business importing a service to log somebody in, and because a failed
    login has no actor to attach: the row carries the *attempted* address, which
    is the whole value of the entry, and no foreign key because there may be no
    such user.
    """
    service = AuditService(db)
    if user is None and not attempted_email:
        return None
    summary = (
        f"Successful sign-in for {user.email}"
        if user is not None
        else "Failed sign-in attempt"
    )
    try:
        return await service.record(
            action=action,
            entity_type="user",
            actor=user,
            entity_id=getattr(user, "id", None),
            entity_label=attempted_email or getattr(user, "email", None),
            summary=summary,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    except Exception:  # pragma: no cover - defensive
        # The one place a failed audit is tolerated, and only because the caller
        # is about to reject or issue a token: a logging failure must not be the
        # reason a correct password is refused. The rollback matters as much as
        # the catch — a failed flush leaves the session unusable, and the very
        # next thing the request does is commit.
        await db.rollback()
        logger.exception("Failed to record %s audit entry", action.value)
        return None


__all__ = [
    "NOISE_FIELDS",
    "AuditService",
    "record_auth_event",
]
