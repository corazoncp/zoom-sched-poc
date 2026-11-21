import sqlite3
from pathlib import Path
import pytest

from app import create_app, zoom


SAMPLE_CREDS = {
    "account_id": "acc_test_1234",
    "client_id": "cli_test_1234",
    "client_secret": "super_secret_value",
}


@pytest.fixture()
def client(tmp_path: Path):
    db_file = tmp_path / "test_appointments.db"
    app = create_app({"TESTING": True, "DB_PATH": str(db_file), "RATELIMIT_ENABLED": False})
    with app.test_client() as test_client:
        yield test_client, db_file


def test_health_and_index(client):
    test_client, _ = client
    assert test_client.get("/health").status_code == 200
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert b"CONFIGURACI" in resp.data


def test_validation_rejects_blank_credentials(client):
    test_client, _ = client
    resp = test_client.post(
        "/api/zoom/verify-credentials",
        json={"account_id": "   ", "client_id": "abc", "client_secret": "12345"},
    )
    assert resp.status_code == 422


def test_full_booking_flow_idempotent_and_no_secret_persistence(client, monkeypatch):
    test_client, db_file = client
    create_calls = []

    monkeypatch.setattr(zoom, "get_access_token", lambda creds: "mock_token")
    monkeypatch.setattr(zoom, "check_slot_availability", lambda **kwargs: (True, None))

    def fake_create_meeting(**kwargs):
        create_calls.append(kwargs)
        return {
            "id": 987654321,
            "join_url": "https://zoom.us/j/987654321",
            "start_url": "https://zoom.us/s/987654321?zak=abc",
        }

    monkeypatch.setattr(zoom, "create_meeting", fake_create_meeting)
    monkeypatch.setattr(zoom, "delete_meeting", lambda token, mid: None)

    # 1. Verify credentials
    v_resp = test_client.post("/api/zoom/verify-credentials", json=SAMPLE_CREDS)
    assert v_resp.status_code == 200
    assert v_resp.get_json()["valid"] is True

    # 2. Request appointment -> payment_pending
    c_resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "2026-11-25T15:00",
        },
    )
    assert c_resp.status_code == 201
    appt = c_resp.get_json()
    assert appt["status"] == "payment_pending"
    assert appt["session_id"].startswith("ses_")
    assert appt["payment_id"].startswith("pay_")
    assert appt["zoom_meeting_id"] is None

    # 3. Lookup by both session_id and payment_id
    assert test_client.get(f"/api/appointments/{appt['session_id']}").status_code == 200
    assert test_client.get(f"/api/appointments/{appt['payment_id']}").status_code == 200

    # 4. Pay -> creates Zoom meeting and transitions to confirmed
    p_resp = test_client.post(
        f"/api/appointments/{appt['session_id']}/pay",
        json={"credentials": SAMPLE_CREDS},
    )
    assert p_resp.status_code == 200
    confirmed = p_resp.get_json()
    assert confirmed["status"] == "confirmed"
    assert confirmed["zoom_meeting_id"] == "987654321"
    assert len(create_calls) == 1

    # 5. Re-running pay is idempotent (no duplicate Zoom meeting)
    p_resp_2 = test_client.post(
        f"/api/appointments/{appt['payment_id']}/pay",
        json={"credentials": SAMPLE_CREDS},
    )
    assert p_resp_2.status_code == 200
    assert len(create_calls) == 1

    # 6. Verify Zoom credentials are never written to SQLite
    raw_db_text = sqlite3.connect(db_file).execute("SELECT * FROM appointments").fetchall()
    serialized = str(raw_db_text)
    assert "super_secret_value" not in serialized
    assert "cli_test_1234" not in serialized
    assert "acc_test_1234" not in serialized

    # 7. Cleanup deletes record
    d_resp = test_client.delete(
        f"/api/appointments/{appt['session_id']}",
        json={"credentials": SAMPLE_CREDS},
    )
    assert d_resp.status_code == 200
    assert test_client.get(f"/api/appointments/{appt['session_id']}").status_code == 404


def test_slot_conflict_returns_409(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(zoom, "get_access_token", lambda creds: "mock_token")
    monkeypatch.setattr(
        zoom, "check_slot_availability", lambda **kwargs: (False, "Conflicto con 'Otra sesión'")
    )

    resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "2026-11-25T15:00",
        },
    )
    assert resp.status_code == 409
    assert resp.get_json()["available"] is False


def test_rate_limiting_blocks_brute_force(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "ratelimit.db"
    app = create_app({"TESTING": True, "DB_PATH": str(db_file), "RATELIMIT_ENABLED": True})
    monkeypatch.setattr(zoom, "get_access_token", lambda creds: "mock_token")

    with app.test_client() as test_client:
        statuses = [
            test_client.post("/api/zoom/verify-credentials", json=SAMPLE_CREDS).status_code
            for _ in range(12)
        ]
    assert 429 in statuses
