"""Pytest configuration and shared test fixtures.

Provides:
- Database session fixture with cleanup between tests
- FastAPI AsyncClient fixture using ASGITransport
- Auth token fixtures for each user role
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# --- Test environment -------------------------------------------------------
# These must be set BEFORE app.core.config is imported, since Settings reads
# the environment at import time.
#
# * A dedicated test database keeps the developer's working data safe, since
#   the suite drops every table on teardown.
# * bcrypt cost is dropped to the minimum: the suite re-seeds demo users
#   between tests, and cost-12 hashing dominated the total runtime.
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://autofix:autofix@localhost:5432/autofix_test",
)
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["APP_DEBUG"] = "false"

from app import models_registry  # noqa: F401
from app.core.config import settings
from app.core.database import Base, get_session
from app.core.seed import seed_all
from app.main import app

# Create a test-specific async engine (created within session fixture scope)
_test_engine = None
TestSessionFactory = None

# Tables managed during cleanup (in dependency order)
_CLEAN_TABLES = [
    "audit_logs",
    "notifications",
    "payments",
    "invoice_items",
    "invoices",
    "purchase_order_items",
    "purchase_orders",
    "suppliers",
    "inventory_transactions",
    "qc_check_items",
    "qc_photos",
    "quality_checks",
    "labor_records",
    "part_requests",
    "repair_tasks",
    "repair_orders",
    "estimate_items",
    "estimates",
    "inspection_photos",
    "inspection_items",
    "inspections",
    "appointments",
    "check_ins",
    "service_requests",
    "vehicle_mileage_records",
    "vehicles",
    "addresses",
    "customers",
    "parts",
    "user_roles",
    "role_permissions",
    "users",
    "permissions",
    "roles",
]


@pytest.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create test engine, tables, and seed data for the test session."""
    global _test_engine, TestSessionFactory

    _test_engine = create_async_engine(
        settings.DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )

    TestSessionFactory = async_sessionmaker(
        _test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )

    # Create all tables
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Clear anything an earlier run left behind, before seeding.
    #
    # `create_all` is a no-op on tables that already exist, so a suite killed
    # half way through -- or a developer who pointed `scripts/seed.py` at the test
    # database -- would otherwise start from the last run's leftovers. That is
    # not a theoretical problem: the demo seed is idempotent by design, so a
    # surviving customer row makes it report "already present" and skip, and the
    # seed tests fail on an assertion about rows that were never written. Each
    # test cleans up after itself, but a run that was interrupted cannot.
    async with TestSessionFactory() as session:
        for table in _CLEAN_TABLES:
            await session.execute(text(f"DELETE FROM {table} CASCADE"))
        await session.commit()

    # Seed the database
    async with TestSessionFactory() as session:
        await seed_all(session)

    yield

    # Cleanup. Table order is resolved from the foreign keys, so
    # inspection_photos is dropped before inspection_items without help.
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _test_engine.dispose()


@pytest.fixture()
async def db() -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session for each test with cleanup after."""
    async with TestSessionFactory() as session:
        yield session
        for table in _CLEAN_TABLES:
            await session.execute(text(f"DELETE FROM {table} CASCADE"))
        await session.commit()
        await seed_all(session)


def _make_session_override(session: AsyncSession):
    """Create a get_session dependency override."""
    async def _override() -> AsyncGenerator[AsyncSession, None]:
        yield session
    return _override


@pytest.fixture()
async def client(db: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Unauthenticated FastAPI test client."""
    app.dependency_overrides[get_session] = _make_session_override(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _get_auth_token(session: AsyncSession, email: str) -> str:
    """Get an auth access token for a demo user."""
    from app.auth.services import AuthService

    auth_service = AuthService(session)
    user = await auth_service.get_user_by_email(email)
    if not user:
        raise RuntimeError(f"Demo user not found: {email}")
    access_token, _ = auth_service.generate_tokens(user)
    return access_token


@pytest.fixture()
async def owner_client(db: AsyncSession):
    """Authenticated test client as Owner."""
    token = await _get_auth_token(db, "owner@autofix.demo")
    app.dependency_overrides[get_session] = _make_session_override(db)
    transport = ASGITransport(app=app)
    ac = AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    yield ac
    await ac.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def manager_client(db: AsyncSession):
    """Authenticated test client as Service Advisor."""
    token = await _get_auth_token(db, "manager@autofix.demo")
    app.dependency_overrides[get_session] = _make_session_override(db)
    transport = ASGITransport(app=app)
    ac = AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    yield ac
    await ac.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def technician_client(db: AsyncSession):
    """Authenticated test client as Technician."""
    token = await _get_auth_token(db, "tech@autofix.demo")
    app.dependency_overrides[get_session] = _make_session_override(db)
    transport = ASGITransport(app=app)
    ac = AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    yield ac
    await ac.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def parts_client(db: AsyncSession):
    """Authenticated test client as Parts Staff."""
    token = await _get_auth_token(db, "parts@autofix.demo")
    app.dependency_overrides[get_session] = _make_session_override(db)
    transport = ASGITransport(app=app)
    ac = AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    yield ac
    await ac.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def customer_client(db: AsyncSession):
    """Authenticated test client as Customer."""
    token = await _get_auth_token(db, "customer@autofix.demo")
    app.dependency_overrides[get_session] = _make_session_override(db)
    transport = ASGITransport(app=app)
    ac = AsyncClient(
        transport=transport, base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    yield ac
    await ac.aclose()
    app.dependency_overrides.clear()


@pytest.fixture()
async def unauth_client():
    """Unauthenticated test client."""
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://testserver")
    yield ac
    await ac.aclose()
