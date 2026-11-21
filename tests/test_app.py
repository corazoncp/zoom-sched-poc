import sqlite3
import threading
from pathlib import Path
import pytest

from app import create_app, zoom


class FakeResponse:
    def __init__(self, status_code: int = 200, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._json_data = json_data
        self.text = text

    def json(self):
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data


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

    def fake_post(url, **kwargs):
        if url == "https://zoom.us/oauth/token":
            return FakeResponse(200, {"access_token": "mock_token"})
        if url == "https://api.zoom.us/v2/users/me/meetings":
            create_calls.append(kwargs.get("json"))
            return FakeResponse(
                201,
                {
                    "id": 987654321,
                    "join_url": "https://zoom.us/j/987654321",
                    "start_url": "https://zoom.us/s/987654321?zak=abc",
                },
            )
        raise AssertionError(f"Unexpected POST URL: {url}")

    def fake_get(url, **kwargs):
        if url == "https://api.zoom.us/v2/users/me/meetings":
            return FakeResponse(200, {"meetings": []})
        raise AssertionError(f"Unexpected GET URL: {url}")

    def fake_delete(url, **kwargs):
        if url == "https://api.zoom.us/v2/meetings/987654321":
            return FakeResponse(204, {})
        raise AssertionError(f"Unexpected DELETE URL: {url}")

    monkeypatch.setattr(zoom.requests, "post", fake_post)
    monkeypatch.setattr(zoom.requests, "get", fake_get)
    monkeypatch.setattr(zoom.requests, "delete", fake_delete)

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

    def fake_post(url, **kwargs):
        if url == "https://zoom.us/oauth/token":
            return FakeResponse(200, {"access_token": "mock_token"})
        raise AssertionError(f"Unexpected POST URL: {url}")

    def fake_get(url, **kwargs):
        if url == "https://api.zoom.us/v2/users/me/meetings":
            return FakeResponse(
                200,
                {
                    "meetings": [
                        {
                            "topic": "Otra sesión",
                            "start_time": "2026-11-25T20:00:00Z",
                            "duration": 60,
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected GET URL: {url}")

    monkeypatch.setattr(zoom.requests, "post", fake_post)
    monkeypatch.setattr(zoom.requests, "get", fake_get)

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
    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, {"access_token": "mock_token"}),
    )

    with app.test_client() as test_client:
        statuses = [
            test_client.post("/api/zoom/verify-credentials", json=SAMPLE_CREDS).status_code
            for _ in range(12)
        ]
    assert 429 in statuses


def test_zoom_invalid_json_returns_502(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, ValueError("Invalid JSON"), text="not json"),
    )
    resp = test_client.post("/api/zoom/verify-credentials", json=SAMPLE_CREDS)
    assert resp.status_code == 502


def test_check_slot_availability_handles_none_duration(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, {"access_token": "mock_token"}),
    )
    monkeypatch.setattr(
        zoom.requests,
        "get",
        lambda url, **kwargs: FakeResponse(
            200,
            {
                "meetings": [
                    {
                        "topic": "Reunión sin duración",
                        "start_time": "2026-11-25T20:00:00Z",
                        "duration": None,
                    }
                ]
            },
        ),
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


def test_check_slot_availability_paginates_via_next_page_token(client, monkeypatch):
    test_client, _ = client
    seen_params = []

    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, {"access_token": "mock_token"}),
    )

    def fake_get(url, **kwargs):
        params = kwargs.get("params", {})
        seen_params.append(dict(params))
        if not params.get("next_page_token"):
            return FakeResponse(
                200,
                {
                    "meetings": [
                        {
                            "topic": "Página 1",
                            "start_time": "2026-11-24T10:00:00Z",
                            "duration": 60,
                        }
                    ],
                    "next_page_token": "tok_page_2",
                },
            )
        return FakeResponse(
            200,
            {
                "meetings": [
                    {
                        "topic": "Conflicto Página 2",
                        "start_time": "2026-11-25T20:00:00Z",
                        "duration": 60,
                    }
                ],
                "next_page_token": "",
            },
        )

    monkeypatch.setattr(zoom.requests, "get", fake_get)

    resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "2026-11-25T15:00",
        },
    )
    assert resp.status_code == 409
    assert "Conflicto Página 2" in resp.get_json()["error"]
    assert len(seen_params) == 2
    assert seen_params[1].get("next_page_token") == "tok_page_2"


def test_create_meeting_missing_required_keys_returns_502(client, monkeypatch):
    test_client, _ = client

    def fake_post(url, **kwargs):
        if url == "https://zoom.us/oauth/token":
            return FakeResponse(200, {"access_token": "mock_token"})
        if url == "https://api.zoom.us/v2/users/me/meetings":
            return FakeResponse(201, {"id": 12345})  # missing join_url, start_url
        raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(zoom.requests, "post", fake_post)
    monkeypatch.setattr(
        zoom.requests,
        "get",
        lambda url, **kwargs: FakeResponse(200, {"meetings": []}),
    )

    c_resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "2026-11-25T15:00",
        },
    )
    assert c_resp.status_code == 201
    sid = c_resp.get_json()["session_id"]

    p_resp = test_client.post(
        f"/api/appointments/{sid}/pay",
        json={"credentials": SAMPLE_CREDS},
    )
    assert p_resp.status_code == 502


@pytest.mark.parametrize(
    ("meeting", "expected_status"),
    [
        # req is 20:00Z..21:00Z; req_end == m_start (21:00Z) -> available
        ({"topic": "After", "start_time": "2026-11-25T21:00:00Z", "duration": 60}, 201),
        # req_start == m_end (19:00Z + 60m = 20:00Z) -> available
        ({"topic": "Before", "start_time": "2026-11-25T19:00:00Z", "duration": 60}, 201),
        # 1-minute overlap at end (20:59Z < 21:00Z) -> conflict
        ({"topic": "Overlap End", "start_time": "2026-11-25T20:59:00Z", "duration": 60}, 409),
        # 1-minute overlap at start (19:01Z + 60m = 20:01Z > 20:00Z) -> conflict
        ({"topic": "Overlap Start", "start_time": "2026-11-25T19:01:00Z", "duration": 60}, 409),
        # Missing start_time is ignored -> available
        ({"topic": "No Start Time", "duration": 60}, 201),
    ],
)
def test_check_slot_availability_boundaries(client, monkeypatch, meeting, expected_status):
    test_client, _ = client
    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, {"access_token": "mock_token"}),
    )
    monkeypatch.setattr(
        zoom.requests,
        "get",
        lambda url, **kwargs: FakeResponse(200, {"meetings": [meeting]}),
    )

    resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "2026-11-25T15:00",
            "timezone": "America/Bogota",
            "duration_minutes": 60,
        },
    )
    assert resp.status_code == expected_status


def test_check_slot_availability_invalid_timezone_returns_400(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, {"access_token": "mock_token"}),
    )
    resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "2026-11-25T15:00",
            "timezone": "Invalid/Timezone",
        },
    )
    assert resp.status_code == 400


def test_check_slot_availability_invalid_datetime_returns_400(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        zoom.requests,
        "post",
        lambda url, **kwargs: FakeResponse(200, {"access_token": "mock_token"}),
    )
    resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "María Perez",
            "start_time_local": "not-a-valid-datetime",
            "timezone": "America/Bogota",
        },
    )
    assert resp.status_code == 400


def test_db_init_sets_wal_once_not_on_every_connection(tmp_path: Path):
    from app import db

    db_file = tmp_path / "wal_test.db"
    db.init_db(db_file)
    with db.get_connection(db_file) as conn:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"

    raw_conn = sqlite3.connect(db_file)
    raw_conn.execute("PRAGMA journal_mode=DELETE;")
    raw_conn.close()

    with db.get_connection(db_file) as conn:
        mode_after = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode_after.lower() == "delete"


def test_duration_minutes_persisted_and_forwarded_to_zoom(client, monkeypatch):
    test_client, _ = client
    captured_payloads = []

    def fake_post(url, **kwargs):
        if url == "https://zoom.us/oauth/token":
            return FakeResponse(200, {"access_token": "mock_token"})
        if url == "https://api.zoom.us/v2/users/me/meetings":
            captured_payloads.append(kwargs.get("json"))
            return FakeResponse(
                201,
                {
                    "id": 11223344,
                    "join_url": "https://zoom.us/j/11223344",
                    "start_url": "https://zoom.us/s/11223344?zak=xyz",
                },
            )
        raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(zoom.requests, "post", fake_post)
    monkeypatch.setattr(
        zoom.requests,
        "get",
        lambda url, **kwargs: FakeResponse(200, {"meetings": []}),
    )

    c_resp = test_client.post(
        "/api/appointments",
        json={
            "credentials": SAMPLE_CREDS,
            "client_name": "Ana López",
            "start_time_local": "2026-11-25T15:00",
            "duration_minutes": 45,
        },
    )
    assert c_resp.status_code == 201
    appt = c_resp.get_json()
    assert appt["duration_minutes"] == 45

    g_resp = test_client.get(f"/api/appointments/{appt['session_id']}")
    assert g_resp.status_code == 200
    assert g_resp.get_json()["duration_minutes"] == 45

    p_resp = test_client.post(
        f"/api/appointments/{appt['session_id']}/pay",
        json={"credentials": SAMPLE_CREDS},
    )
    assert p_resp.status_code == 200
    assert p_resp.get_json()["duration_minutes"] == 45
    assert len(captured_payloads) == 1
    assert captured_payloads[0]["duration"] == 45


def test_concurrent_pay_creates_only_one_zoom_meeting(tmp_path: Path, monkeypatch):
    db_file = tmp_path / "concurrent_pay.db"
    app = create_app({"TESTING": True, "DB_PATH": str(db_file), "RATELIMIT_ENABLED": False})

    create_calls = []
    in_create = threading.Event()
    allow_create_finish = threading.Event()

    def fake_post(url, **kwargs):
        if url == "https://zoom.us/oauth/token":
            return FakeResponse(200, {"access_token": "mock_token"})
        if url == "https://api.zoom.us/v2/users/me/meetings":
            create_calls.append(kwargs.get("json"))
            in_create.set()
            assert allow_create_finish.wait(timeout=2.0)
            return FakeResponse(
                201,
                {
                    "id": 555666777,
                    "join_url": "https://zoom.us/j/555666777",
                    "start_url": "https://zoom.us/s/555666777?zak=abc",
                },
            )
        raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(zoom.requests, "post", fake_post)
    monkeypatch.setattr(
        zoom.requests,
        "get",
        lambda url, **kwargs: FakeResponse(200, {"meetings": []}),
    )

    with app.test_client() as tc:
        c_resp = tc.post(
            "/api/appointments",
            json={
                "credentials": SAMPLE_CREDS,
                "client_name": "María Perez",
                "start_time_local": "2026-11-25T15:00",
            },
        )
        assert c_resp.status_code == 201
        sid = c_resp.get_json()["session_id"]

    statuses = []

    def worker_1():
        with app.test_client() as tc1:
            r = tc1.post(f"/api/appointments/{sid}/pay", json={"credentials": SAMPLE_CREDS})
            statuses.append(r.status_code)

    def worker_2():
        assert in_create.wait(timeout=2.0)
        with app.test_client() as tc2:
            r = tc2.post(f"/api/appointments/{sid}/pay", json={"credentials": SAMPLE_CREDS})
            statuses.append(r.status_code)
        allow_create_finish.set()

    t1 = threading.Thread(target=worker_1)
    t2 = threading.Thread(target=worker_2)
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert len(create_calls) == 1
    assert sorted(statuses) == [200, 409]


def test_index_validates_http_urls_before_rendering_links(client):
    test_client, _ = client
    resp = test_client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "function safeHttpUrl(" in html
    assert 'parsed.protocol === "http:" || parsed.protocol === "https:"' in html
    assert "safeHttpUrl(appt.zoom_join_url)" in html
    assert "safeHttpUrl(appt.zoom_start_url)" in html
    assert 'href="${escapeHtml(appt.zoom_join_url)}"' not in html
    assert 'href="${escapeHtml(appt.zoom_start_url)}"' not in html
