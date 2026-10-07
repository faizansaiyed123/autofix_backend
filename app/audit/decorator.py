"""The route decorator that writes audit entries.

Auditing at the route boundary rather than through global ORM hooks is a
deliberate trade. An ORM hook sees every write, including ones made by scripts,
migrations and background jobs, which sounds like a virtue and is in practice a
liability: it cannot know *who* did the write, because the session is the only
thing it is handed. The route boundary is the one place where the actor, the
request and the outcome are all simultaneously known, so that is where entries
are written.

What the decorator does:

1. Reads the "before" snapshot of the target row, before the handler runs.
2. Runs the handler untouched.
3. Reads the "after" state from the response the handler returned — which is the
   best available description of the new truth, because it is exactly what the
   caller was told.
4. Writes one entry carrying the diff, in the handler's transaction.

What it does not do: swallow failures. If the entry cannot be written the
request fails. An audit trail that records "we tried to record this" is not a
trail.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any

from fastapi import Depends, Request
from pydantic import BaseModel

from app.audit.models import AuditAction
from app.audit.services import AuditService
from app.core.database import AsyncSession, get_session

logger = logging.getLogger("autofix.audit.decorator")

# Route handlers take a path parameter whose name ends in `_id`; that is how the
# decorator finds the row it is about. A create route has no such parameter,
# which is how it knows there is no "before" to read.
_ID_PARAM_SUFFIX = "_id"

# Plain-language verbs for the one-line summary. A summary built by string-munging
# the enum produces "Create d a customer"; a table produces something a person
# can read at a glance, and this is the only place the two vocabularies meet.
_VERBS: dict[AuditAction, str] = {
    AuditAction.CREATE: "Created",
    AuditAction.UPDATE: "Updated",
    AuditAction.DELETE: "Deleted",
    AuditAction.STATUS_CHANGE: "Changed status of",
    AuditAction.APPROVE: "Approved",
    AuditAction.REJECT: "Rejected",
    AuditAction.SEND: "Sent",
    AuditAction.ISSUE: "Issued",
    AuditAction.DECIDE: "Recorded a decision on",
    AuditAction.RECORD_PAYMENT: "Recorded a payment on",
    AuditAction.VOID_PAYMENT: "Voided a payment on",
    AuditAction.REFUND: "Refunded",
    AuditAction.LOGIN: "Signed in",
    AuditAction.LOGIN_FAILED: "Failed sign-in attempt for",
    AuditAction.LOGOUT: "Signed out",
    AuditAction.PERMISSION_CHANGE: "Changed permissions on",
    AuditAction.EXPORT: "Exported",
}


def audit(
    action: AuditAction,
    entity_type: str,
    *,
    summary: str | None = None,
    id_param: str | None = None,
    label_attr: str | None = None,
    commit: bool = True,
) -> Callable[[Callable], Callable]:
    """Record an audit entry for a successful call to a route.

    Args:
        action: What the person did, from the constrained vocabulary.
        entity_type: The registry key — ``customer``, ``invoice``, and so on.
        summary: Overrides the generated one-liner.
        id_param: Path parameter holding the row's id. Inferred from the handler
            signature when omitted, which is right for every audited route here.
        label_attr: Field on the response to use as the entity label. Falls back
            to the registry's label fields for the entity type.
        commit: Whether the decorator commits the entry. On by default: a route
            handler that has returned a response has finished its unit of work,
            and the entry belongs in the same transaction as the change.
    """

    def decorator(handler: Callable) -> Callable:
        params = inspect.signature(handler).parameters
        target_param = id_param or _infer_id_param(params)
        return _build_wrapper(
            handler,
            action=action,
            entity_type=entity_type,
            summary=summary,
            target_param=target_param,
            label_attr=label_attr,
            commit=commit,
        )
    return decorator


def _build_wrapper(
    handler: Callable,
    *,
    action: AuditAction,
    entity_type: str,
    summary: str | None,
    target_param: str | None,
    label_attr: str | None,
    commit: bool,
) -> Callable:
    handler_params = inspect.signature(handler).parameters
    injected = _missing_injections(handler_params)

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        session: AsyncSession | None = kwargs.get("session")
        if session is None:
            # Raising is correct: a route wired to the decorator without a
            # session is a wiring mistake, and quietly not auditing it would
            # hide the mistake permanently.
            raise RuntimeError(
                "@audit requires the route to declare a `session` parameter"
            )
        actor = kwargs.get("current_user")
        request: Request | None = kwargs.get("request")

        service = AuditService(session)
        entity_id = kwargs.get(target_param) if target_param else None
        # Read the "before" state first. A delete has no other chance to see what
        # it deleted, and taking it after the handler runs would snapshot the
        # change instead of the thing that changed.
        before_state = (
            await service.snapshot_entity(entity_type, entity_id)
            if target_param and entity_id is not None
            else None
        )

        # The injected parameters are for the decorator, not the handler. Handing
        # them on would be a TypeError on every route that did not declare them,
        # which is to say on every route this decorator is actually for.
        result = await handler(*args, **{k: v for k, v in kwargs.items() if k not in injected})

        after_state = _state_from_result(result)
        if target_param and entity_id is not None and not _is_entity_response(after_state):
            # Either the handler returned nothing (a 204, and the row may well
            # still exist: several "deletes" here are soft deletes that flip a
            # status), or it returned something that is not the record itself —
            # a decision result, a wrapper, a message. In both cases the database
            # is the authority on what the row now holds, so ask it.
            after_state = await service.snapshot_entity(entity_type, entity_id)
        resolved_id = _resolve_entity_id(entity_id, after_state)
        entry = await service.record_change(
            action=action,
            entity_type=entity_type,
            entity_id=resolved_id,
            actor=actor,
            before_state=before_state,
            after_state=after_state,
            summary=summary or default_summary(action, entity_type),
            entity_label=_label(result, after_state, label_attr),
            ip_address=client_ip(request),
            user_agent=_user_agent(request),
        )
        if commit:
            await session.commit()
            await session.refresh(entry)
        return result

    wrapper.__signature__ = _extend_signature(
        inspect.signature(handler), _missing_injections(inspect.signature(handler).parameters)
    )
    return wrapper


# --- signature surgery -----------------------------------------------------


def _extend_signature(
    signature: inspect.Signature, injected: dict[str, inspect.Parameter]
) -> inspect.Signature:
    """Append the parameters the decorator needs but the handler did not declare.

    Two details make this work, and both are load-bearing:

    * They are appended **after** the handler's own parameters, because a
      parameter with a default cannot precede one without, and several of these
      handlers end in defaulted query parameters.
    * Every injected parameter is given a default. FastAPI replaces that default
      with the resolved request or dependency before the handler is called, so
      the value is never actually ``None`` in a real request; the default exists
      only to satisfy Python's ordering rule.
    """
    if not injected:
        return signature
    params = list(signature.parameters.values())
    for param in injected.values():
        params.append(param.replace(default=param.default))
    return signature.replace(parameters=params)


def _missing_injections(params: Any) -> dict[str, inspect.Parameter]:
    """Parameters the decorator adds if the handler does not already have them.

    ``request`` carries the IP address and user agent. ``session`` and
    ``current_user`` are a safety net only: every audited route in this codebase
    already declares both, because they are already required for authorisation.
    """
    injected: dict[str, inspect.Parameter] = {}
    if "request" not in params:
        injected["request"] = inspect.Parameter(
            "request",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Request,
            default=None,
        )
    if "session" not in params:
        injected["session"] = inspect.Parameter(
            "session",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=AsyncSession,
            default=Depends(get_session),
        )
    if "current_user" not in params:
        from app.auth.dependencies import get_current_active_user

        injected["current_user"] = inspect.Parameter(
            "current_user",
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Any,
            default=Depends(get_current_active_user),
        )
    return injected


def _infer_id_param(params: Any) -> str | None:
    """The path parameter naming the row, e.g. ``customer_id``."""
    for name in params:
        if name.endswith(_ID_PARAM_SUFFIX):
            return name
    return None


# --- result inspection -----------------------------------------------------


def _state_from_result(result: Any) -> dict | None:
    """The "after" state, taken from whatever the handler returned.

    A response model is the best available description of the new state: it is
    the exact set of fields the caller was told are true, already filtered to
    the ones this system publishes. Re-reading the row instead would risk
    recording columns the API deliberately hides.
    """
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, dict):
        return result
    return None


def _is_entity_response(state: dict | None) -> bool:
    """Whether a response body is the record itself rather than something about it.

    An ``id`` is the test: a route that hands back the entity carries the
    entity's own key, and a route that hands back a result, a count or a message
    does not. Guessing from the route name would have been wrong somewhere.
    """
    return state is not None and bool(state.get("id"))


def _resolve_entity_id(fallback: Any, after_state: dict | None) -> Any:
    """The id to file the entry under.

    Prefers the id from the response, because a create's id only ever exists
    there, and falls back to the path parameter for updates and deletes whose
    response may not carry one.
    """
    if after_state and after_state.get("id"):
        return after_state["id"]
    return fallback


def _label(result: Any, after_state: dict | None, label_attr: str | None) -> str | None:
    """The human-facing reference, when the handler made one easy to reach.

    ``None`` is fine: the service falls back to the registry's label fields for
    the entity type, which is a better answer than a route guessing.
    """
    if label_attr and isinstance(result, BaseModel):
        value = getattr(result, label_attr, None)
        if value:
            return str(value)[:200]
    return None


def default_summary(action: AuditAction, entity_type: str) -> str:
    """A one-line description in plain words, e.g. ``"Created a customer"``.

    Carries no row identity, because the entity label beside it does that job
    and two half-specific fields make one vague one.
    """
    verb = _VERBS.get(action, action.value.replace("_", " ").capitalize())
    article = "an" if entity_type[:1].lower() in "aeiou" else "a"
    return f"{verb} {article} {entity_type.replace('_', ' ')}"


def client_ip(request: Request | None) -> str | None:
    """The caller's address, preferring a proxy header when one is set.

    Only the leftmost entry of ``X-Forwarded-For``: that is the client as the
    first proxy saw it, and everything after it is infrastructure the shop owns.
    """
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    if request.client is not None:
        return request.client.host[:45]
    return None


def _user_agent(request: Request | None) -> str | None:
    if request is None:
        return None
    return request.headers.get("user-agent")


__all__ = ["audit", "client_ip", "default_summary"]
