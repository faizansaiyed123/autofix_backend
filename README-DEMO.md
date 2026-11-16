# AutoFix isolated demo API

This branch contains a standalone FastAPI demonstration entry point:

\`uvicorn app.demo_api:app --reload --host 127.0.0.1 --port 8001\`

Open \`http://127.0.0.1:8001/docs\` for its API schema.

## Demo accounts

Every account uses the password \`demo1234\`.

| Role | Email |
| --- | --- |
| Owner | \`owner@autofix.demo\` |
| Service advisor | \`manager@autofix.demo\` |
| Technician | \`tech@autofix.demo\` |
| Parts staff | \`parts@autofix.demo\` |
| Customer | \`customer@autofix.demo\` |

## Isolation boundary

- The API is implemented in \`app/demo_api.py\` and does not import \`app.main\`, database configuration, SQLAlchemy models, migrations, the production seeders, or the email worker.
- Records are synthetic and held in process memory. Restarting the process restores the original sample records.
- Demo bearer tokens are intentionally simple and are not suitable for production authentication.
- Registration, password recovery, email delivery, and payment processing are illustrative only. No message is sent and no money is moved.
- This API is separate from the frontend's browser-local demo adapter. The frontend can run as a recruiter-facing demo without requiring this API server.

Do not use this entry point for real customer data, production traffic, or security testing.
