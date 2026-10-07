"""API routes for the audit log.

Read-only, and that is the whole design. There is no POST, no PUT and no DELETE
in this router, because an endpoint that can erase audit rows is an endpoint for
erasing evidence, and no role in this system holds one. Pruning a log that has
grown past what anybody will ever read is an operator decision made at the
database, in the open.

Every route requires ``audit_logs:read``, which only the OWNER holds. An audit
log is a record of what one person did to another person's record, and there is
no shop where the front desk is entitled to read it.

Endpoints:
- GET /                  The log, newest first, with filters
- GET /actions           The action vocabulary, for populating a filter dropdown
- GET /entity-types      The registered entity types, same reason
- GET /{log_id}          One entry, with full before/after state
- GET /entity/{type}/{id}  Everything that happened to one record
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.models import AUDIT_ACTION_VALUES
from app.audit.registry import registered_types
from app.audit.schemas import (
    AuditEntityHistory,
    AuditLogList,
    AuditLogRead,
    AuditLogSummary,
)
from app.audit.services import AuditService
from app.auth.dependencies import require_permission
from app.auth.models import User
from app.auth.permissions import PermissionEnum
from app.core.database import get_session

router = APIRouter()


def get_audit_service(db: AsyncSession = Depends(get_session)) -> AuditService:
    return AuditService(db)


@router.get("/", response_model=AuditLogList)
async def list_audit_logs(
    actor_id: uuid.UUID | None = Query(None, description="Only this actor's entries"),
    action: str | None = Query(None, description="One action, e.g. APPROVE"),
    entity_type: str | None = Query(None),
    entity_id: uuid.UUID | None = Query(None),
    date_from: date | None = Query(None),
    date_to: date | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_permission(PermissionEnum.AUDIT_LOGS_READ)),
    service: AuditService = Depends(get_audit_service),
):
    """The log, newest first.

    Filters combine with AND. Anything not supplied is not filtered, so the
    unfiltered call is the whole log and a filtered one is a narrower question
    about it.
    """
    entries, total = await service.list_logs(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        date_from=date_from,
        date_to=date_to,
        page=page,
        size=size,
    )
    return AuditLogList.create(
        [AuditLogSummary.from_row(e) for e in entries], page, size, total
    )


@router.get("/actions")
async def list_actions(
    current_user: User = Depends(require_permission(PermissionEnum.AUDIT_LOGS_READ)),
):
    """The action vocabulary, so a client does not hard-code it.

    Read from the model rather than a copy: the ``CHECK`` constraint on the
    column is generated from the same tuple, so a client that renders this list
    can never offer an action the database would reject.
    """
    return {"actions": list(AUDIT_ACTION_VALUES)}


@router.get("/entity-types")
async def list_entity_types(
    current_user: User = Depends(require_permission(PermissionEnum.AUDIT_LOGS_READ)),
):
    """Entity types this build can snapshot, for the same reason.

    Smaller and more honest than the list of *audited* types: it says what the
    registry knows how to read, which is exactly what a client needs to decide
    whether a detail view can show full state.
    """
    return {"entity_types": list(registered_types())}


@router.get("/entity/{entity_type}/{entity_id}", response_model=AuditEntityHistory)
async def get_entity_history(
    entity_type: str,
    entity_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(require_permission(PermissionEnum.AUDIT_LOGS_READ)),
    service: AuditService = Depends(get_audit_service),
):
    """Everything that has happened to one record, newest first.

    Declared before ``/{log_id}`` so the two-segment path can never be captured
    by the one-segment route.
    """
    entries = await service.entity_history(entity_type, entity_id, limit=limit)
    label = entries[0].entity_label if entries else None
    return AuditEntityHistory(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=label,
        total=len(entries),
        entries=[AuditLogSummary.from_row(e) for e in entries],
    )


@router.get("/{log_id}", response_model=AuditLogRead)
async def get_audit_log(
    log_id: uuid.UUID,
    current_user: User = Depends(require_permission(PermissionEnum.AUDIT_LOGS_READ)),
    service: AuditService = Depends(get_audit_service),
):
    """One entry, including the full before and after snapshots.

    The list endpoint trims these; this one does not, because the whole reason
    somebody clicked through to a single entry is to see the states.
    """
    return AuditLogRead.from_row(await service.get(log_id))


__all__ = ["router"]
