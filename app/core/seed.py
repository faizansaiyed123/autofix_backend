"""Seed data creation for roles, permissions, and demo users.

This module provides functions to populate the database with initial
RBAC data and demo accounts. Used by both the seed CLI script and
test fixtures.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import Permission, Role, RolePermission, User, UserRole
from app.auth.permissions import ROLE_PERMISSIONS, PermissionEnum, RoleEnum
from app.core.security import hash_password

logger = logging.getLogger("autofix.seed")

# Demo user data: (email, first_name, last_name, password, role)
DEMO_USERS: list[tuple[str, str, str, str, RoleEnum]] = [
    ("owner@autofix.demo", "John", "Smith", "demo1234", RoleEnum.OWNER),
    ("manager@autofix.demo", "Sarah", "Johnson", "demo1234", RoleEnum.SERVICE_ADVISOR),
    ("tech@autofix.demo", "Mike", "Rodriguez", "demo1234", RoleEnum.TECHNICIAN),
    ("parts@autofix.demo", "Lisa", "Chen", "demo1234", RoleEnum.PARTS_STAFF),
    ("customer@autofix.demo", "David", "Wilson", "demo1234", RoleEnum.CUSTOMER),
]

# Seed passwords for testing
TEST_PASSWORD = "demo1234"


async def seed_roles(session: AsyncSession) -> list[Role]:
    """Create all roles if they don't exist."""
    roles: list[Role] = []

    existing_stmt = select(Role)
    existing_result = await session.execute(existing_stmt)
    existing_roles = {r.name: r for r in existing_result.scalars()}

    for role_enum in RoleEnum:
        if role_enum.value not in existing_roles:
            role = Role(
                name=role_enum.value,
                description=f"{role_enum.value} role",
            )
            session.add(role)
            roles.append(role)
            logger.info("Created role: %s", role_enum.value)
        else:
            roles.append(existing_roles[role_enum.value])

    await session.flush()
    return roles


async def seed_permissions(session: AsyncSession) -> list[Permission]:
    """Create all permissions if they don't exist."""
    permissions: list[Permission] = []

    existing_stmt = select(Permission)
    existing_result = await session.execute(existing_stmt)
    existing_perms = {p.name: p for p in existing_result.scalars()}

    for perm_enum in PermissionEnum:
        if perm_enum.value not in existing_perms:
            perm = Permission(
                name=perm_enum.value,
                description=perm_enum.value.replace("_", " ").replace(":", " "),
            )
            session.add(perm)
            permissions.append(perm)
            logger.info("Created permission: %s", perm_enum.value)
        else:
            permissions.append(existing_perms[perm_enum.value])

    await session.flush()
    return permissions


async def seed_role_permissions(
    session: AsyncSession,
    roles_by_name: dict[str, Role],
    perms_by_name: dict[str, Permission],
) -> None:
    """Create role-permission mappings."""
    # Check existing mappings
    existing_stmt = select(RolePermission)
    existing_result = await session.execute(existing_stmt)
    existing_count = len(existing_result.fetchall())

    if existing_count > 0:
        logger.info("Role permissions already seeded (%d records)", existing_count)
        return

    for role_enum, perm_enums in ROLE_PERMISSIONS.items():
        role = roles_by_name[role_enum.value]
        for perm_enum in perm_enums:
            perm = perms_by_name[perm_enum.value]
            rp = RolePermission(role_id=role.id, permission_id=perm.id)
            session.add(rp)
            logger.debug("Granted %s -> %s", role_enum.value, perm_enum.value)

    logger.info("Seeded role permissions")


async def seed_users(
    session: AsyncSession,
    roles_by_name: dict[str, Role],
) -> list[User]:
    """Create demo users with assigned roles."""
    users: list[User] = []

    for email, first_name, last_name, password, role_enum in DEMO_USERS:
        # Check if user already exists
        existing = await session.execute(
            select(User).where(User.email == email)
        )
        if existing.scalar_one_or_none():
            logger.info("Demo user already exists: %s", email)
            continue

        user = User(
            email=email,
            password_hash=hash_password(password),
            first_name=first_name,
            last_name=last_name,
            is_active=True,
            is_staff=role_enum != RoleEnum.CUSTOMER,
        )
        session.add(user)
        await session.flush()

        # Assign role
        role = roles_by_name[role_enum.value]
        ur = UserRole(user_id=user.id, role_id=role.id)
        session.add(ur)

        users.append(user)
        logger.info("Created demo user: %s (%s)", email, role_enum.value)

    return users


async def seed_all(session: AsyncSession) -> None:
    """Run all seed operations in the correct order."""
    logger.info("Starting seed...")

    roles = await seed_roles(session)
    permissions = await seed_permissions(session)

    roles_by_name = {r.name: r for r in roles}
    perms_by_name = {p.name: p for p in permissions}

    await seed_role_permissions(session, roles_by_name, perms_by_name)
    await seed_users(session, roles_by_name)

    await session.commit()
    logger.info("Seed complete!")


async def get_role(session: AsyncSession, role_name: str) -> Role | None:
    """Fetch a role by name."""
    result = await session.execute(select(Role).where(Role.name == role_name))
    return result.scalar_one_or_none()


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    """Fetch a user by email with their roles."""
    result = await session.execute(
        select(User)
        .where(User.email == email)
        .limit(1)
    )
    return result.scalar_one_or_none()
