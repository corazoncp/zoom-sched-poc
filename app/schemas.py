from typing import Literal
from pydantic import BaseModel, Field, SecretStr, field_validator


class ZoomCredentials(BaseModel):
    """Transient Zoom Server-to-Server OAuth credentials (never persisted)."""

    account_id: str = Field(min_length=4, max_length=128)
    client_id: str = Field(min_length=4, max_length=128)
    client_secret: SecretStr = Field(min_length=4, max_length=256)

    @field_validator("account_id", "client_id")
    @classmethod
    def strip_non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Field cannot be blank")
        return cleaned


class AppointmentCreateRequest(BaseModel):
    """Request payload to check Zoom availability and create a pending appointment."""

    credentials: ZoomCredentials
    client_name: str = Field(min_length=2, max_length=120)
    start_time_local: str = Field(min_length=16, max_length=32)
    timezone: str = Field(default="America/Bogota", min_length=3, max_length=64)
    duration_minutes: int = Field(default=60, ge=15, le=240)
    amount_usd: float = Field(default=50.0, gt=0, le=10000)

    @field_validator("client_name")
    @classmethod
    def clean_client_name(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 2:
            raise ValueError("Client name must be at least 2 characters")
        return cleaned


class PayAppointmentRequest(BaseModel):
    """Request payload to register mock payment and provision the Zoom meeting."""

    credentials: ZoomCredentials


class DeleteAppointmentRequest(BaseModel):
    """Request payload to delete a confirmed Zoom meeting and local appointment."""

    credentials: ZoomCredentials


class AppointmentResponse(BaseModel):
    """Public representation of an appointment stored in SQLite."""

    session_id: str
    payment_id: str
    client_name: str
    start_time_local: str
    timezone: str
    duration_minutes: int = 60
    amount_usd: float
    status: Literal["payment_pending", "confirmed"]
    zoom_meeting_id: str | None = None
    zoom_join_url: str | None = None
    zoom_start_url: str | None = None
    created_at: str
    paid_at: str | None = None
