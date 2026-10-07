"""AutoFix - Auto Repair Garage Management Platform.

Main FastAPI application entry point.
"""

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
from app.customers.routes import router as customers_router
from app.estimates.routes import router as estimates_router
from app.inspections.routes import router as inspections_router
from app.notifications.routes import router as notifications_router
from app.service_requests.routes import router as service_requests_router
from app.vehicles.routes import router as vehicles_router


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.COMPANY_NAME,
        description="AutoFix - Auto Repair Garage Management & Operations Platform",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.ALLOWED_ORIGINS or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    api_prefix = "/api/v1"

    @app.on_event("startup")
    async def startup() -> None:
        await init_db()

    @app.on_event("shutdown")
    async def shutdown() -> None:
        await close_engine()

    @app.get("/health", tags=["Health"])
    async def health_check():
        return {"status": "healthy", "app": "AutoFix"}

    # The shop's own endpoints are shop-wide: `GET /invoices/` is the invoice
    # book, `GET /vehicles/` is every vehicle on the premises. They are mounted
    # behind the staff gate rather than left to their permissions alone, because a
    # customer legitimately holds several of those permissions for the portal and
    # a permission says nothing about whose rows the caller may read.
    #
    # Three routers are deliberately outside the gate, and each is scoped to the
    # signed-in user rather than to the shop: auth (it is how a caller becomes
    # anybody at all), the portal (the customer's own account, one row set), and
    # notifications (each query filters on `current_user.id`, so a customer sees
    # their own badge and nobody else's).
    # Called, not merely referenced: `require_staff` builds the dependency, the
    # same way `require_permission(...)` does at a route. Handing FastAPI the
    # factory instead would type-check, return a function nobody calls, and leave
    # the gate open — a guard that looks fitted and does nothing.
    staff_only = [Depends(require_staff())]

    app.include_router(auth_router, prefix=f"{api_prefix}/auth", tags=["auth"])
    app.include_router(
        notifications_router, prefix=f"{api_prefix}/notifications", tags=["notifications"]
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
    # Read-only, and last: an audit log is not part of any day's work, it is the
    # record of what happened during one.
    app.include_router(
        audit_router, prefix=f"{api_prefix}/audit", tags=["audit"], dependencies=staff_only
    )

    return app


app = create_app()
