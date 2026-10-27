# AutoFix Backend

AutoFix Backend is the API service for AutoFix, an auto-repair garage management application. It uses FastAPI, SQLAlchemy's asynchronous ORM, PostgreSQL, and Alembic migrations. The versioned REST API supports workshop operations and a customer portal.

**Visual product walkthrough:** [View the AutoFix frontend walkthrough](https://github.com/faizansaiyed123/autofix_frontend/blob/main/docs/testing/VISUAL-TEST-WALKTHROUGH.md).

## Key features

### Authentication and access control
- Customer registration, email verification, and password recovery.
- JWT access and refresh tokens, token rotation, and logout.
- Role-based access control with role and permission checks.
- Database-backed authentication rate limiting.

### Workshop operations
- Customer, vehicle, and mileage management.
- Appointment scheduling, calendar views, and conflict checks.
- Vehicle check-ins and digital inspections, including inspection reports and photo records.
- Repair orders, task tracking, and technician labor records.
- Quality-control checks, re-verification, and rework workflows.
- Service requests and technician-to-parts-staff requests.

### Parts, inventory, and purchasing
- Parts catalog and inventory movement ledger.
- Stock-level summaries and low-stock alerts.
- Supplier management and purchase orders.
- Delivery receiving and inventory updates.

### Estimates, invoices, and payments
- Estimates with line items, pricing, discounts, and tax calculations.
- Customer approval or rejection of individual estimate lines.
- Invoice issuance, balances, payment records, and payment voiding.
- Customer-facing estimate and invoice documents.

### Customer portal, notifications, and reporting
- Customer access to their vehicles, appointments, service requests, estimates, invoices, and payment history.
- Appointment booking and invoice payment through the portal API.
- In-app notifications and unread counts.
- Audit logs and reports for revenue, repair orders, technician productivity, inventory value, and customer retention.

## Tech stack and architecture

- **Language:** Python 3.11 or later, as declared in `pyproject.toml`.
- **API:** FastAPI and Uvicorn.
- **Database:** PostgreSQL, SQLAlchemy's asynchronous ORM, and `asyncpg`.
- **Schema migrations:** Alembic.
- **Authentication:** JWT through `python-jose`, with bcrypt password hashing and role-based permissions.
- **Configuration:** Pydantic Settings and environment variables.
- **Email:** SMTP delivery with a PostgreSQL-backed email outbox and a background worker for delivery and retries.
- **Testing and development:** pytest, pytest-asyncio, HTTPX, factory-boy, Faker, Ruff, and mypy.

The API is mounted under `/api/v1`. The application also exposes a `/health` endpoint.

Authentication and email safeguards include token rotation, single-use verification and recovery tokens, shared database-backed rate limits, and an email outbox that stores job metadata rather than live verification or password-reset links.

## Companion repository

- [AutoFix Frontend](https://github.com/faizansaiyed123/autofix_frontend)

The frontend provides the web interface for workshop operations and customer self-service.

## Quick start

### Prerequisites

- Python 3.11 or later.
- A PostgreSQL database reachable from the backend. The CI workflow uses PostgreSQL 16.
- SMTP configuration if email verification and password recovery are required.
- Docker, if running the containerized backend.

### Local development

Create and activate a virtual environment, then install the project and its development dependencies:

```bash
python -m venv .venv

# macOS/Linux
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows PowerShell, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

Create a local environment file from the provided example:

```bash
# macOS/Linux
cp .env.example .env
```

```powershell
# Windows PowerShell
Copy-Item .env.example .env
```

Edit `.env` locally to configure the database connection, application secret, allowed origins, and any required email settings. Do not commit secrets.

Apply the database migrations and start the API:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

With the API running locally, FastAPI's interactive documentation is available at `http://127.0.0.1:8000/docs`, and the health endpoint is available at `http://127.0.0.1:8000/health`.

### Docker

Build and run the backend image:

```bash
docker build -t autofix-backend .
docker run --rm --env-file .env -p 8000:8000 autofix-backend
```

Before starting the container, configure `.env` so `APP_HOST` is reachable through the container's published port and `DATABASE_URL` points to a PostgreSQL instance reachable from the container.

The Docker entrypoint waits for the database, applies Alembic migrations, optionally runs the seed utility, and starts Uvicorn.

The repository does not include a Docker Compose file.

## Environment variables

The following names are declared in `.env.example`. Values are intentionally omitted.

**Application**

`APP_ENV`, `APP_DEBUG`, `APP_HOST`, `APP_PORT`

**Database**

`DATABASE_URL`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`, `DATABASE_POOL_SIZE`, `DATABASE_MAX_OVERFLOW`, `DATABASE_POOL_TIMEOUT`

**Authentication and security**

`SECRET_KEY`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_DAYS`, `BCRYPT_ROUNDS`, `ALLOWED_ORIGINS`, `AUTH_RATE_LIMIT_TRUST_PROXY_HEADERS`

**Email and delivery**

`SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`, `SMTP_USE_STARTTLS`, `SMTP_USE_SSL`, `SMTP_TIMEOUT_SECONDS`, `PUBLIC_APP_URL`, `EMAIL_OUTBOX_BATCH_SIZE`, `EMAIL_OUTBOX_POLL_SECONDS`, `EMAIL_OUTBOX_MAX_ATTEMPTS`, `EMAIL_OUTBOX_LOCK_SECONDS`

**Company settings, uploads, and seeding**

`COMPANY_NAME`, `COMPANY_CURRENCY`, `COMPANY_TAX_RATE`, `MAX_UPLOAD_SIZE_MB`, `AUTOFIX_SEED`

**Frontend integration and ports**

`BACKEND_PORT`, `FRONTEND_PORT`, `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_CURRENCY`, `NEXT_PUBLIC_COMPANY_NAME`

## Tests

Install the development dependencies as described above, configure a dedicated PostgreSQL test database, and run:

```bash
pytest -q
```

The test suite covers API behavior, authentication, permissions, business-state transitions, database migrations, inventory and financial rules, notifications, and end-to-end workflows.

The GitHub Actions workflow at `.github/workflows/backend-ci.yml` applies database migrations against PostgreSQL 16 before running `pytest -q`.

**Important:** Use a disposable test database. The test fixtures manage and clean database tables; do not run the tests against production data.

## Project structure

```text
autofix_backend/
├── app/
│   ├── auth/                  # Authentication and permissions
│   ├── appointments/          # Scheduling and conflicts
│   ├── audit/                 # Audit logging
│   ├── checkins/              # Vehicle check-ins
│   ├── customers/             # Customer management
│   ├── vehicles/              # Vehicles and mileage
│   ├── service_requests/      # Workshop service requests
│   ├── inspections/           # Digital inspections
│   ├── estimates/             # Estimates and customer decisions
│   ├── repair_orders/         # Repair orders and tasks
│   ├── labor/                 # Technician labor records
│   ├── qc/                    # Quality control
│   ├── parts/                 # Parts catalog
│   ├── inventory/             # Inventory ledger
│   ├── part_requests/         # Internal part requests
│   ├── suppliers/             # Supplier management
│   ├── purchase_orders/       # Purchase orders and receiving
│   ├── invoices/              # Invoices
│   ├── payments/              # Payment records
│   ├── portal/                # Customer portal API
│   ├── notifications/         # In-app notifications
│   ├── reports/               # Reports and analytics
│   ├── users/                 # User management
│   ├── core/                  # Configuration, database, email, security
│   ├── common/                # Shared dependencies and utilities
│   ├── main.py                # Application setup and router registration
│   └── models_registry.py     # SQLAlchemy model registry
├── alembic/
│   └── versions/              # Database migrations
├── docs/
│   └── production-auth-and-email.md
├── scripts/
│   └── seed.py                # Optional database seeding utility
├── tests/                     # Automated tests and shared fixtures
├── .github/workflows/
│   └── backend-ci.yml
├── Dockerfile
├── docker-entrypoint.sh
├── alembic.ini
├── pyproject.toml
└── .env.example
```

## Documentation

- [Production authentication and email delivery](docs/production-auth-and-email.md) — required settings, deployment order, security safeguards, and operational boundaries.
- [Backend CI workflow](.github/workflows/backend-ci.yml) — migration and test execution.
- [Frontend visual product walkthrough](https://github.com/faizansaiyed123/autofix_frontend/blob/main/docs/testing/VISUAL-TEST-WALKTHROUGH.md) — screenshots and user-facing workflows.
