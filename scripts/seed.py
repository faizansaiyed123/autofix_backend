"""CLI script to seed the database.

Usage:
    python scripts/seed.py            # roles, permissions, logins, and demo work
    python scripts/seed.py --no-demo  # roles, permissions and logins only

Creates:
- All roles (OWNER, SERVICE_ADVISOR, TECHNICIAN, PARTS_STAFF, CUSTOMER)
- All permissions
- Role-permission mappings
- Demo users for each role
- Unless --no-demo: customers, vehicles, catalog, work in progress, and money
  both taken and owed

Demo accounts, all with the password ``demo1234``:

    owner@autofix.demo     John Smith      OWNER
    manager@autofix.demo   Sarah Johnson   SERVICE_ADVISOR
    tech@autofix.demo      Mike Rodriguez  TECHNICIAN
    parts@autofix.demo     Lisa Chen       PARTS_STAFF
    customer@autofix.demo  David Wilson    CUSTOMER

What signing in as each one shows:

    owner      every screen, including the audit log and the two reports that
               rank named people
    manager    the front desk: the repair-order board, estimates waiting to be
               sent, the overdue invoice to chase
    technician the car on the lift, their own labour records, the jobs they
               have finished
    parts      the catalog and the stock ledger, with three low-stock alerts
               waiting to be reordered
    customer   the portal: two cars, an estimate awaiting a decision, one
               invoice settled and one still owed

``customer@autofix.demo`` is the account the portal is designed around. It is the
only demo customer with a login, which is deliberate: the portal resolves the
customer from the token, so two of them would give it a choice to make, and a
portal that picks the wrong one shows somebody their neighbour's invoices.

**Re-running is safe.** The demo data is written once and only once: if the
portal customer already exists the seed reports that and writes nothing, rather
than doubling every invoice. ``--no-demo`` skips the business data entirely and
leaves the logins alone, which is the flag to reach for when you want a clean
authentication test without a shop's worth of rows around it.
"""

import asyncio
import os
import sys

# Add backend to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Every model, not the handful this script happens to touch. SQLAlchemy resolves
# relationships by name at mapper configuration time, so a module that references
# a class it does not import — Supplier referencing PurchaseOrder — fails the first
# time a relationship is configured. The application and the test suite both get
# this for free by importing the registry; a standalone script has to ask.
from app import models_registry  # noqa: F401
from app.core.database import AsyncSessionFactory, close_engine, init_db
from app.core.demo_data import seed_demo_data
from app.core.seed import seed_all


async def main(include_demo: bool = True):
    await init_db()

    async with AsyncSessionFactory() as session:
        await seed_all(session)

        if include_demo:
            # Committed separately from the RBAC seed on purpose: the demo data
            # is disposable and slow to reason about, and being able to skip it
            # without losing the logins is the difference between a useful flag
            # and a confusing one.
            result = await seed_demo_data(session)
            await session.commit()
            print(result)

    await close_engine()


if __name__ == "__main__":
    demo = "--no-demo" not in sys.argv
    asyncio.run(main(include_demo=demo))
