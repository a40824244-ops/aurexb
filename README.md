# AUREX FastAPI API

FastAPI service for the AUREX storefront. It serves the product catalog and
deals, records WhatsApp orders, consultation requests, and VIP subscriptions,
and provides protected administration endpoints. `fastapi[standard]` installs
the FastAPI CLI used by FastAPI Cloud. This folder is independently deployable
as the `aurexb` repository backend.

## Run locally

From this folder (or from the root of the standalone backend repository):

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and set unique, strong values for `aDMIN_PASSWORD` and
`sECRET_KEY` (generate a key with
`py -c "import secrets; print(secrets.token_urlsafe(48))"`). Then start the API:

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 5000
```

The API listens at `http://localhost:5000`; interactive API documentation is
available at `http://localhost:5000/docs`. The SQLite database is created at
`aurex.sqlite3` and is seeded from `seed_data.json` the first time it
runs.

The React app uses `http://localhost:5000/api` by default. In the original
full-project checkout, `npm run backend` is a shortcut to start this API.
Set `VITE_API_BASE_URL` in the frontend `.env` file to use a different API URL.
Configure `cORS_ORIGINS` in `.env` as a comma-separated list when the frontend
is served from a different origin.

## API

| Method | Endpoint | Access |
| --- | --- | --- |
| `GET` | `/api/health` | Public |
| `POST` | `/api/auth/login` | Public |
| `GET` | `/api/auth/verify` | Admin |
| `GET` | `/api/products` | Public |
| `GET` | `/api/products/{id}` | Public |
| `POST`, `PUT`, `DELETE` | `/api/products` | Admin |
| `GET` | `/api/deals` | Public |
| `POST`, `PUT`, `DELETE` | `/api/deals` | Admin |
| `POST` | `/api/orders` | Public |
| `GET` | `/api/orders` | Admin |
| `PATCH` | `/api/orders/{id}/status` | Admin |
| `POST` | `/api/consultations` | Public |
| `GET` | `/api/consultations` | Admin |
| `POST` | `/api/vip` | Public |
| `GET` | `/api/dashboard/stats` | Admin |

Admin routes accept the short-lived token returned from login in the
`x-admin-token` header. Never commit `.env` or the SQLite database.

Run the backend tests from this folder after installing the
requirements:

```powershell
.\.venv\Scripts\python -m pytest
```
