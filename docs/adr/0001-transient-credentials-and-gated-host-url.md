# ADR-0001: Transient Zoom Credentials and Gated Host URL

- **Status:** Accepted
- **Date:** 2026-11-21
- **Context:** `zoom-sched-poc` is a standalone proof-of-concept demo allowing a host to test end-to-end Zoom scheduling with a Server-to-Server OAuth app without deploying an authentication layer or persisting third-party secrets on the server.

## Decision
1. **Transient browser-memory credentials:** Zoom `account_id`, `client_id`, and `client_secret` are entered in Screen 1, held solely in browser tab memory, and transmitted per request over HTTPS (`SecretStr` in Pydantic). They are never stored in SQLite, session cookies, or server environment files.
2. **Gated `zoom_start_url`:** Unauthenticated `GET /api/appointments/<lookup_id>` omits `zoom_start_url` (which embeds a Zoom host ZAK token) and returns only client-safe metadata (`zoom_join_url`, `zoom_meeting_id`). `zoom_start_url` is returned only from `POST /api/appointments/<lookup_id>/pay` after verifying the caller's Zoom credentials via `zoom.get_access_token`.

## Consequences
- **Positive:** Eliminates server-side secret storage and prevents anyone holding a `session_id`/`payment_id` from hijacking host controls via unauthenticated `GET` lookup.
- **Negative / Trade-off:** Refreshing the browser tab requires re-entering Zoom credentials in Screen 1, and Screen 3 `BUSCAR` lookup alone does not display the host link unless payment is confirmed in the same flow.
