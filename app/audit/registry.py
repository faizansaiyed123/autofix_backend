"""Which models can be snapshotted, and how each one should be labelled.

The audit table stores *state*, and state has to come from somewhere. Handing
every audited route a hand-written ``{"phone": ...}`` dict would work right up
until somebody adds a field and forgets, and the resulting log would claim to be
a snapshot while quietly missing the one value that matters.

So each entity type registers the SQLAlchemy model it stands for plus the fields
that identify it to a human. Everything else is read straight off the model.

Registration is explicit and at import time rather than discovered by walking
``Base.metadata``: two tables can share a name, and a snapshot that picked the
wrong one would be worse than no snapshot at all.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class EntitySpec:
    """How to read one entity type for an audit entry."""

    __slots__ = ("entity_type", "label_fields", "model", "sensitive_fields")

    def __init__(
        self,
        entity_type: str,
        model: type,
        label_fields: tuple[str, ...],
        sensitive_fields: frozenset[str] = frozenset(),
    ):
        self.entity_type = entity_type
        self.model = model
        # Tried in order; the first one with a value wins.
        self.label_fields = label_fields
        # Never written into a snapshot: passwords, hashes, tokens, and anything
        # else that must not end up in a table anybody can browse.
        self.sensitive_fields = sensitive_fields

    def __repr__(self) -> str:
        return f"<EntitySpec {self.entity_type} -> {self.model.__tablename__}>"


_REGISTRY: dict[str, EntitySpec] = {}


def register(
    entity_type: str,
    model: type,
    label_fields: tuple[str, ...],
    sensitive_fields: frozenset[str] = frozenset(),
) -> EntitySpec:
    """Register (or re-register) an entity type for auditing."""
    spec = EntitySpec(entity_type, model, label_fields, sensitive_fields)
    _REGISTRY[entity_type] = spec
    return spec


def get_spec(entity_type: str) -> EntitySpec | None:
    """The spec for an entity type, or ``None`` if it is not registered.

    ``None`` is a normal answer, not an error: an unregistered entity still gets
    an audit row, it just gets one with no before/after state attached.
    """
    return _REGISTRY.get(entity_type)


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


async def load_state(
    session: AsyncSession, entity_type: str, entity_id: Any
) -> dict | None:
    """Read one row as a JSON-ready dict, or ``None`` if it is gone.

    ``None`` is the interesting case and is not an error: a delete has an
    "after" state of nothing, and that is exactly what the log should say.
    """
    spec = get_spec(entity_type)
    if spec is None or entity_id is None:
        return None
    result = await session.execute(
        select(spec.model).where(spec.model.id == str(entity_id))
    )
    row = result.scalar_one_or_none()
    if row is None:
        return None
    return snapshot(spec, row)


def snapshot(spec: EntitySpec, row: Any) -> dict:
    """Turn a model instance into a JSON-serialisable dict, minus secrets."""
    state: dict[str, Any] = {}
    for column in spec.model.__table__.columns:
        name = column.name
        if name in spec.sensitive_fields:
            continue
        value = getattr(row, name, None)
        state[name] = _jsonable(value)
    return state


def label_for(state: dict | None, spec: EntitySpec | None) -> str | None:
    """The human-facing reference for a row, e.g. an invoice number.

    Tried in order, because every entity's best label is a different field, and
    an entry that falls back to the primary key is still far better than one with
    no label at all.
    """
    if not state or spec is None:
        return None
    for field in spec.label_fields:
        value = state.get(field)
        if value not in (None, ""):
            return str(value)[:200]
    return None


def _jsonable(value: Any) -> Any:
    """Coerce a column value into something ``json.dumps`` will accept.

    UUIDs, decimals and datetimes all appear in these models and none of them
    survive a JSONB column unchanged; leaving them to the driver produces a row
    that reads back as a string anyway, and a row that cannot be written at all
    for the rest.
    """
    import datetime as _dt
    import decimal
    import uuid as _uuid

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def diff_states(
    before: dict | None, after: dict | None, *, ignore: frozenset[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """A readable field-by-field diff between two snapshots.

    Returns ``{"status": {"from": "DRAFT", "to": "ISSUED"}}`` rather than a
    before/after pair of whole rows: a diff is what somebody actually reads, and
    showing forty unchanged fields to report one changed one is how an audit log
    gets ignored.

    When both snapshots exist, only fields **present in both** are compared. The
    two sides are not always the same shape — one is read from the table, the
    other from the response a route returned — and a key that appears on one side
    alone is a difference in what was serialised, not a change to the record.
    Counting it as one turns every update into a diff listing every column the
    API happens to expose, which is how a real change gets lost in the noise.

    A field genuinely added to or removed from the *schema* therefore stops being
    diffed, which is the intended trade: the full states are stored beside the
    diff for exactly that case.
    """
    if before is None and after is None:
        return {}
    if before is None:
        return {
            k: {"from": None, "to": v} for k, v in (after or {}).items() if k not in ignore
        }
    if after is None:
        return {k: {"from": v, "to": None} for k, v in before.items() if k not in ignore}

    shared = (set(before) & set(after)) - ignore
    changes: dict[str, dict[str, Any]] = {}
    for key in shared:
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changes[key] = {"from": old, "to": new}
    return changes


def register_standard_entities() -> None:
    """Register the entities this shop audits.

    Imported for its side effect from :mod:`app.audit`, so the registry is the
    same wherever it is used from.
    """
    from app.auth.models import User
    from app.customers.models import Customer
    from app.estimates.models import Estimate
    from app.invoices.models import Invoice
    from app.payments.models import Payment
    from app.repair_orders.models import RepairOrder
    from app.vehicles.models import Vehicle

    register("customer", Customer, ("email", "first_name", "last_name"))
    register("vehicle", Vehicle, ("vin", "license_plate", "make"))
    register("user", User, ("email",), frozenset({"password_hash"}))
    register("invoice", Invoice, ("invoice_number", "status"))
    # Payments have no number of their own; the card or receipt reference is what
    # a person would recognise on a statement.
    register("payment", Payment, ("reference", "status"))
    register("repair_order", RepairOrder, ("ro_number", "status"))
    register("estimate", Estimate, ("estimate_number", "status"))


__all__ = [
    "EntitySpec",
    "diff_states",
    "get_spec",
    "label_for",
    "load_state",
    "register",
    "register_standard_entities",
    "registered_types",
    "snapshot",
]
