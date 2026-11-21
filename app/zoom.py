import base64
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import requests

from app.schemas import ZoomCredentials


class ZoomAPIError(Exception):
    """Raised when Zoom returns an HTTP error or invalid payload."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _extract_zoom_error(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data.get("message") or data.get("reason") or resp.text
    except ValueError:
        pass
    return resp.text or f"Zoom API HTTP {resp.status_code}"


def get_access_token(creds: ZoomCredentials) -> str:
    """Exchange Server-to-Server OAuth credentials for a short-lived Bearer token."""
    raw = f"{creds.client_id}:{creds.client_secret.get_secret_value()}".encode("utf-8")
    b64 = base64.b64encode(raw).decode("utf-8")

    try:
        resp = requests.post(
            "https://zoom.us/oauth/token",
            params={"grant_type": "account_credentials", "account_id": creds.account_id},
            headers={"Authorization": f"Basic {b64}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ZoomAPIError(502, f"Network error contacting Zoom OAuth: {exc}") from exc

    if not resp.ok:
        raise ZoomAPIError(resp.status_code, _extract_zoom_error(resp))

    token = resp.json().get("access_token")
    if not token:
        raise ZoomAPIError(502, "Zoom OAuth response did not include an access_token")
    return token


def check_slot_availability(
    access_token: str,
    start_time_local: str,
    tz_name: str,
    duration_minutes: int = 60,
) -> tuple[bool, str | None]:
    """Check upcoming Zoom meetings for overlapping time windows."""
    try:
        local_tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise ZoomAPIError(400, f"Invalid timezone: {tz_name}") from exc

    try:
        parsed = datetime.fromisoformat(start_time_local)
    except ValueError as exc:
        raise ZoomAPIError(400, "Invalid date/time format. Expected YYYY-MM-DDTHH:MM") from exc

    req_start = (parsed if parsed.tzinfo else parsed.replace(tzinfo=local_tz)).astimezone(timezone.utc)
    req_end = req_start + timedelta(minutes=duration_minutes)

    try:
        resp = requests.get(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"type": "upcoming", "page_size": 100},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ZoomAPIError(502, f"Network error contacting Zoom Meetings API: {exc}") from exc

    if not resp.ok:
        raise ZoomAPIError(resp.status_code, _extract_zoom_error(resp))

    for meeting in resp.json().get("meetings", []):
        raw_start = meeting.get("start_time")
        if not raw_start:
            continue
        m_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        m_end = m_start + timedelta(minutes=meeting.get("duration", 60))
        if req_start < m_end and req_end > m_start:
            topic = meeting.get("topic", "Reunión existente")
            return False, f"Conflicto con '{topic}' ({raw_start})"

    return True, None


def create_meeting(
    access_token: str,
    topic: str,
    start_time_local: str,
    tz_name: str,
    duration_minutes: int = 60,
) -> dict:
    """Create a scheduled Zoom meeting on the account owner's calendar."""
    try:
        resp = requests.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "topic": topic,
                "type": 2,
                "start_time": start_time_local,
                "duration": duration_minutes,
                "timezone": tz_name,
                "settings": {
                    "host_video": True,
                    "participant_video": True,
                    "join_before_host": False,
                    "mute_upon_entry": True,
                    "waiting_room": True,
                },
            },
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ZoomAPIError(502, f"Network error creating Zoom meeting: {exc}") from exc

    if not resp.ok:
        raise ZoomAPIError(resp.status_code, _extract_zoom_error(resp))

    return resp.json()


def delete_meeting(access_token: str, meeting_id: str) -> None:
    """Delete a Zoom meeting by ID (used for demo cleanup)."""
    try:
        resp = requests.delete(
            f"https://api.zoom.us/v2/meetings/{meeting_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise ZoomAPIError(502, f"Network error deleting Zoom meeting: {exc}") from exc

    if not resp.ok and resp.status_code != 404:
        raise ZoomAPIError(resp.status_code, _extract_zoom_error(resp))
