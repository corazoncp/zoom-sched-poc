import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = os.environ.get("DB_PATH", "data/appointments.db")


def get_db_path() -> Path:
    return Path(os.environ.get("DB_PATH", DEFAULT_DB_PATH))


@contextmanager
def get_connection(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    target = db_path or get_db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    """Create the appointments table if it does not already exist."""
    with get_connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                session_id TEXT PRIMARY KEY,
                payment_id TEXT UNIQUE NOT NULL,
                client_name TEXT NOT NULL,
                start_time_local TEXT NOT NULL,
                timezone TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL DEFAULT 60,
                amount_usd REAL NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('payment_pending', 'payment_processing', 'confirmed')),
                zoom_meeting_id TEXT,
                zoom_join_url TEXT,
                zoom_start_url TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
            """
        )
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(appointments)").fetchall()}
        if "duration_minutes" not in cols:
            conn.execute(
                "ALTER TABLE appointments ADD COLUMN duration_minutes INTEGER NOT NULL DEFAULT 60"
            )


def _row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def create_appointment(
    client_name: str,
    start_time_local: str,
    timezone_name: str,
    amount_usd: float,
    duration_minutes: int = 60,
    db_path: Path | None = None,
) -> dict:
    """Insert a new appointment in payment_pending state."""
    session_id = f"ses_{secrets.token_hex(5)}"
    payment_id = f"pay_{secrets.token_hex(5)}"
    created_at = datetime.now(timezone.utc).isoformat()

    with get_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO appointments (
                session_id, payment_id, client_name, start_time_local,
                timezone, duration_minutes, amount_usd, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'payment_pending', ?)
            """,
            (
                session_id,
                payment_id,
                client_name,
                start_time_local,
                timezone_name,
                duration_minutes,
                amount_usd,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM appointments WHERE session_id = ?", (session_id,)
        ).fetchone()
    return dict(row)


def get_appointment_by_lookup(lookup_id: str, db_path: Path | None = None) -> dict | None:
    """Fetch an appointment by either session_id or payment_id."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE session_id = ? OR payment_id = ?",
            (lookup_id, lookup_id),
        ).fetchone()
    return _row_to_dict(row)


def claim_appointment_for_payment(session_id: str, db_path: Path | None = None) -> bool:
    """Atomically claim a payment_pending appointment for Zoom meeting provisioning."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE appointments
            SET status = 'payment_processing'
            WHERE session_id = ? AND status = 'payment_pending'
            """,
            (session_id,),
        )
    return cursor.rowcount == 1


def release_payment_claim(session_id: str, db_path: Path | None = None) -> None:
    """Revert an in-flight payment_processing claim back to payment_pending."""
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE appointments
            SET status = 'payment_pending'
            WHERE session_id = ? AND status = 'payment_processing'
            """,
            (session_id,),
        )


def mark_appointment_confirmed(
    session_id: str,
    zoom_meeting_id: str,
    zoom_join_url: str,
    zoom_start_url: str,
    db_path: Path | None = None,
) -> dict | None:
    """Transition an appointment to confirmed and store Zoom session metadata."""
    paid_at = datetime.now(timezone.utc).isoformat()
    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE appointments
            SET status = 'confirmed',
                zoom_meeting_id = ?,
                zoom_join_url = ?,
                zoom_start_url = ?,
                paid_at = ?
            WHERE session_id = ?
            """,
            (zoom_meeting_id, zoom_join_url, zoom_start_url, paid_at, session_id),
        )
        row = conn.execute(
            "SELECT * FROM appointments WHERE session_id = ?", (session_id,)
        ).fetchone()
    return _row_to_dict(row)


def delete_appointment(session_id: str, db_path: Path | None = None) -> bool:
    """Remove an appointment record from SQLite unless payment is in flight."""
    with get_connection(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM appointments WHERE session_id = ? AND status != 'payment_processing'",
            (session_id,),
        )
    return cursor.rowcount > 0
