# zoom-sched-poc

Interactive 3-screen presentation proof-of-concept for **El Corazón como Portal** demonstrating Zoom Server-to-Server OAuth scheduling gated by payment confirmation.

## Features
- **3-screen presentation flow** with automatic smooth scrolling:
  1. **Configuración (Servidor):** Validates Zoom Server-to-Server OAuth credentials in memory (`SecretStr` in Pydantic; never persisted to SQLite or disk).
  2. **Solicitar una Cita (Como cliente):** Checks Zoom upcoming meetings for conflicts (`GET /v2/users/me/meetings`), then creates a `payment_pending` appointment in SQLite and returns `session_id` + `payment_id`.
  3. **Buscar Sesión:** Looks up `session_id` or `payment_id`, simulates payment confirmation, provisions the Zoom meeting (`POST /v2/users/me/meetings`), and displays session links idempotently on repeat lookups.
- **Brute-force protection:** `Flask-Limiter` per-IP rate limits (with `ProxyFix` for Traefik) and 40-bit random `ses_`/`pay_` IDs.

## Local Development

```bash
uv sync
uv run flask --app "app:create_app()" run --debug --port 8000
```

Run tests:

```bash
uv run pytest
```

## Single-Container Docker Run

```bash
docker build -t zoom-sched-poc .
docker run -p 8000:8000 -v zoom_sched_data:/app/data zoom-sched-poc
```
