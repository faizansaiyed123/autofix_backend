"""AutoFix - Auto Repair Garage Management Platform.

Main FastAPI application entry point.
"""

import asyncio

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging(settings.APP_ENV)

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.appointments.routes import router as appointments_router
from app.audit.routes import router as audit_router
from app.auth.dependencies import require_staff
from app.auth.routes import router as auth_router
from app.checkins.routes import router as checkins_router
from app.core.database import close_engine, init_db
from app.core.email import run_email_outbox_worker
from app.customers.routes import router as customers_router
from app.estimates.routes import router as estimates_router
from app.inspections.routes import router as inspections_router
from app.inventory.routes import router as inventory_router
from app.invoices.routes import router as invoices_router
from app.labor.routes import router as labor_router
from app.notifications.routes import router as notifications_router
from app.part_requests.routes import router as part_requests_router
from app.parts.routes import router as parts_router
from app.payments.routes import router as payments_router
from app.portal.routes import router as portal_router
from app.purchase_orders.routes import router as purchase_orders_router
from app.qc.routes import router as qc_router
from app.repair_orders.routes import router as repair_orders_router
from app.reports.routes import router as reports_router
from app.service_requests.routes import router as service_requests_router
from app.suppliers.routes import router as suppliers_router
from app.users.routes import router as users_router
from app.vehicles.routes import router as vehicles_router


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.COMPANY_NAME,
        description="AutoFix - Auto Repair Garage Management & Operations Platform",
        version="0.1.0",
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    if settings.is_production and (
        not settings.ALLOWED_ORIGINS or "*" in settings.ALLOWED_ORIGINS
    ):
        raise RuntimeError(
            "Production must configure ALLOWED_ORIGINS with explicit frontend origins; wildcards are unsafe."
        )
    if settings.is_production and settings.APP_DEBUG:
        raise RuntimeError("APP_DEBUG must be false in production.")
    if settings.is_production and len(settings.SECRET_KEY.encode("utf-8")) < 32:
        raise RuntimeError("Production SECRET_KEY must be at least 32 bytes.")
    if settings.is_production and not settings.AUTH_RATE_LIMIT_TRUST_PROXY_HEADERS:
        raise RuntimeError(
            "Production must explicitly enable AUTH_RATE_LIMIT_TRUST_PROXY_HEADERS behind its trusted edge proxy."
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_prefix = "/api/v1"

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.url.path.startswith("/api/v1/auth/"):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response

    @app.on_event("startup")
    async def startup() -> None:
        await init_db()
        stop_event = asyncio.Event()
        app.state.email_outbox_stop = stop_event
        app.state.email_outbox_task = asyncio.create_task(
            run_email_outbox_worker(stop_event),
            name="autofix-email-outbox",
        )

    @app.on_event("shutdown")
    async def shutdown() -> None:
        stop_event = getattr(app.state, "email_outbox_stop", None)
        worker = getattr(app.state, "email_outbox_task", None)
        if stop_event is not None:
            stop_event.set()
        if worker is not None:
            try:
                await asyncio.wait_for(worker, timeout=15)
            except TimeoutError:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
        await close_engine()

    @app.get("/health", tags=["Health"])
    async def health_check():
        return {"status": "healthy", "app": "AutoFix"}

    # Shop-wide routers are staff-only; permissions alone do not scope records to their owner.
    # Auth, portal, and notifications remain outside this gate and enforce user-level scope.
    # Pass require_staff() so FastAPI receives a dependency, not the factory itself.
    staff_only = [Depends(require_staff())]

    app.include_router(auth_router, prefix=f"{api_prefix}/auth", tags=["auth"])
    app.include_router(
        portal_router, prefix=f"{api_prefix}/portal", tags=["portal"]
    )
    app.include_router(
        notifications_router, prefix=f"{api_prefix}/notifications", tags=["notifications"]
    )
    app.include_router(
        users_router, prefix=f"{api_prefix}/users", tags=["users"], dependencies=staff_only
    )
    app.include_router(
        customers_router, prefix=f"{api_prefix}/customers", tags=["customers"],
        dependencies=staff_only,
    )
    app.include_router(
        vehicles_router, prefix=f"{api_prefix}/vehicles", tags=["vehicles"],
        dependencies=staff_only,
    )
    app.include_router(
        service_requests_router, prefix=f"{api_prefix}/service_requests",
        tags=["service_requests"], dependencies=staff_only,
    )
    app.include_router(
        appointments_router, prefix=f"{api_prefix}/appointments", tags=["appointments"],
        dependencies=staff_only,
    )
    app.include_router(
        checkins_router, prefix=f"{api_prefix}/check_ins", tags=["check_ins"],
        dependencies=staff_only,
    )
    app.include_router(
        inspections_router, prefix=f"{api_prefix}/inspections", tags=["inspections"],
        dependencies=staff_only,
    )
    app.include_router(
        estimates_router, prefix=f"{api_prefix}/estimates", tags=["estimates"],
        dependencies=staff_only,
    )
    app.include_router(
        repair_orders_router, prefix=f"{api_prefix}/repair_orders", tags=["repair_orders"],
        dependencies=staff_only,
    )
    app.include_router(
        labor_router, prefix=f"{api_prefix}/labor", tags=["labor"], dependencies=staff_only
    )
    app.include_router(
        part_requests_router, prefix=f"{api_prefix}/part_requests", tags=["part_requests"],
        dependencies=staff_only,
    )
    app.include_router(
        parts_router, prefix=f"{api_prefix}/parts", tags=["parts"], dependencies=staff_only
    )
    app.include_router(
        inventory_router, prefix=f"{api_prefix}/inventory", tags=["inventory"],
        dependencies=staff_only,
    )
    app.include_router(
        suppliers_router, prefix=f"{api_prefix}/suppliers", tags=["suppliers"],
        dependencies=staff_only,
    )
    app.include_router(
        purchase_orders_router, prefix=f"{api_prefix}/purchase_orders", tags=["purchase_orders"],
        dependencies=staff_only,
    )
    app.include_router(
        qc_router, prefix=f"{api_prefix}/qc", tags=["qc"], dependencies=staff_only
    )
    app.include_router(
        invoices_router, prefix=f"{api_prefix}/invoices", tags=["invoices"],
        dependencies=staff_only,
    )
    app.include_router(
        payments_router, prefix=f"{api_prefix}/payments", tags=["payments"],
        dependencies=staff_only,
    )
    app.include_router(
        reports_router, prefix=f"{api_prefix}/reports", tags=["reports"],
        dependencies=staff_only,
    )
    # Read-only, and last: an audit log is not part of any day's work, it is the
    # record of what happened during one.
    app.include_router(
        audit_router, prefix=f"{api_prefix}/audit", tags=["audit"], dependencies=staff_only
    )

    return app


app = create_app()
