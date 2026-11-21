# zoom-sched-poc

Interactive 3-screen presentation proof-of-concept for **El Corazón como Portal** demonstrating Zoom Server-to-Server OAuth scheduling gated by payment confirmation.

## Features
- **3-screen presentation flow** with automatic smooth scrolling:
  1. **Configuración (Servidor):** Validates Zoom Server-to-Server OAuth credentials (`POST /api/zoom/verify-credentials`) using `SecretStr` in Pydantic; credentials stay strictly in browser tab memory and are never persisted to SQLite or disk.
  2. **Solicitar una Cita (Como cliente):** Checks Zoom upcoming meetings for time overlaps (`GET /v2/users/me/meetings`, paginated up to 20 pages), persists a `payment_pending` appointment (including `duration_minutes`) in SQLite, and returns `session_id` + `payment_id`.
  3. **Buscar Sesión:** Looks up `session_id` or `payment_id` (`GET /api/appointments/<lookup_id>`), lets the presenter confirm mock payment (`POST /api/appointments/<lookup_id>/pay`) to provision the Zoom meeting (`POST /v2/users/me/meetings`), and handles repeat lookups idempotently.
- **Security & concurrency invariants:**
  - Unauthenticated `GET /api/appointments/<lookup_id>` omits `zoom_start_url` (host ZAK token); `zoom_start_url` is returned only by `POST /pay` after verifying Zoom credentials.
  - Atomic `payment_processing` claim/release prevents duplicate Zoom meetings on concurrent `/pay` requests, and `DELETE` returns `409 Conflict` while a payment is in flight.
  - `ProxyFix` + `Flask-Limiter` (`memory://`, single Gunicorn worker `--workers 1 --threads 4`), `MAX_CONTENT_LENGTH` (64 KiB), `safeHttpUrl()` link scheme validation, and 40-bit random `ses_`/`pay_` IDs.

## Local Development

```bash
uv sync
uv run flask --app "app:create_app()" run --debug --port 8000
```

Run tests:

```bash
uv run pytest -v
```

## Single-Container Docker Run

The container runs as non-root `appuser` with `--workers 1 --threads 4` (required for authoritative in-memory rate limiting) and a built-in `HEALTHCHECK` on `/health`.

```bash
docker build -t zoom-sched-poc .
docker run -p 8000:8000 -v zoom_sched_data:/app/data zoom-sched-poc
```

## Documentation
- [Architecture Overview](./ARCHITECTURE.md)
- [ADR-0001: Transient Credentials and Gated Host URL](./docs/adr/0001-transient-credentials-and-gated-host-url.md)
