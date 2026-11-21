import os
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from pydantic import BaseModel, ValidationError
from werkzeug.middleware.proxy_fix import ProxyFix

from app import db, zoom
from app.schemas import (
    AppointmentCreateRequest,
    AppointmentResponse,
    DeleteAppointmentRequest,
    PayAppointmentRequest,
    ZoomCredentials,
)


def _parse_body(model_cls: type[BaseModel]):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, (jsonify({"error": "Invalid JSON body"}), 400)
    try:
        return model_cls.model_validate(payload), None
    except ValidationError as exc:
        return None, (jsonify({"error": "Validation failed", "details": exc.errors()}), 422)


def _resolve_appointment(lookup_id: str, db_path: Path | None):
    cleaned = lookup_id.strip()
    if not cleaned or len(cleaned) > 64:
        return None, (jsonify({"error": "ID inválido"}), 400)
    row = db.get_appointment_by_lookup(cleaned, db_path=db_path)
    if not row:
        return None, (jsonify({"error": f"No se encontró ninguna sesión para '{cleaned}'."}), 404)
    return row, None


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="../static")
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    if test_config:
        app.config.update(test_config)

    db_path_cfg = app.config.get("DB_PATH")
    db_path = Path(db_path_cfg) if db_path_cfg else None
    db.init_db(db_path)

    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=["60 per minute"],
        storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
        enabled=app.config.get("RATELIMIT_ENABLED", True),
    )
    app.limiter = limiter

    @app.errorhandler(413)
    def handle_payload_too_large(_exc):
        return jsonify({"error": "El cuerpo de la solicitud excede el límite permitido."}), 413

    @app.errorhandler(429)
    def handle_rate_limit(_exc):
        return (
            jsonify(
                {
                    "error": "Demasiadas solicitudes. Por favor espera un momento antes de intentar de nuevo."
                }
            ),
            429,
        )

    @app.errorhandler(zoom.ZoomAPIError)
    def handle_zoom_error(exc: zoom.ZoomAPIError):
        status = exc.status_code if 400 <= exc.status_code < 600 else 502
        return jsonify({"error": exc.message, "zoom_status": exc.status_code}), status

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify({"status": "ok"}), 200

    @app.post("/api/zoom/verify-credentials")
    @limiter.limit("10 per minute")
    def verify_credentials():
        creds, err = _parse_body(ZoomCredentials)
        if err:
            return err
        zoom.get_access_token(creds)
        return jsonify({"valid": True, "message": "Credenciales de Zoom verificadas."}), 200

    @app.post("/api/appointments")
    @limiter.limit("15 per minute")
    def create_appointment():
        body, err = _parse_body(AppointmentCreateRequest)
        if err:
            return err

        token = zoom.get_access_token(body.credentials)
        available, conflict = zoom.check_slot_availability(
            access_token=token,
            start_time_local=body.start_time_local,
            tz_name=body.timezone,
            duration_minutes=body.duration_minutes,
        )
        if not available:
            return jsonify({"available": False, "error": conflict}), 409

        row = db.create_appointment(
            client_name=body.client_name,
            start_time_local=body.start_time_local,
            timezone_name=body.timezone,
            amount_usd=body.amount_usd,
            duration_minutes=body.duration_minutes,
            db_path=db_path,
        )
        return jsonify(AppointmentResponse.model_validate(row).model_dump()), 201

    @app.get("/api/appointments/<lookup_id>")
    @limiter.limit("20 per minute")
    def get_appointment(lookup_id: str):
        row, err = _resolve_appointment(lookup_id, db_path)
        if err:
            return err
        payload = AppointmentResponse.model_validate(row).model_dump()
        payload["zoom_start_url"] = None
        return jsonify(payload), 200

    @app.post("/api/appointments/<lookup_id>/pay")
    @limiter.limit("10 per minute")
    def pay_appointment(lookup_id: str):
        row, err = _resolve_appointment(lookup_id, db_path)
        if err:
            return err

        body, err = _parse_body(PayAppointmentRequest)
        if err:
            return err

        # Idempotent: return existing confirmed session without creating a duplicate meeting
        if row["status"] == "confirmed":
            zoom.get_access_token(body.credentials)
            return jsonify(AppointmentResponse.model_validate(row).model_dump()), 200
        if row["status"] == "payment_processing":
            return jsonify({"error": "El pago ya está siendo procesado."}), 409

        if not db.claim_appointment_for_payment(row["session_id"], db_path=db_path):
            latest = db.get_appointment_by_lookup(row["session_id"], db_path=db_path)
            if latest and latest["status"] == "confirmed":
                zoom.get_access_token(body.credentials)
                return jsonify(AppointmentResponse.model_validate(latest).model_dump()), 200
            return jsonify({"error": "El pago ya está siendo procesado."}), 409

        try:
            token = zoom.get_access_token(body.credentials)
            meeting = zoom.create_meeting(
                access_token=token,
                topic=f"Sesión con {row['client_name']}",
                start_time_local=row["start_time_local"],
                tz_name=row["timezone"],
                duration_minutes=row["duration_minutes"],
            )
            updated = db.mark_appointment_confirmed(
                session_id=row["session_id"],
                zoom_meeting_id=str(meeting["id"]),
                zoom_join_url=meeting["join_url"],
                zoom_start_url=meeting["start_url"],
                db_path=db_path,
            )
        except Exception:
            db.release_payment_claim(row["session_id"], db_path=db_path)
            raise
        return jsonify(AppointmentResponse.model_validate(updated).model_dump()), 200

    @app.delete("/api/appointments/<lookup_id>")
    @limiter.limit("10 per minute")
    def delete_appointment(lookup_id: str):
        row, err = _resolve_appointment(lookup_id, db_path)
        if err:
            return err

        body, err = _parse_body(DeleteAppointmentRequest)
        if err:
            return err

        if row["status"] == "payment_processing":
            return jsonify({"error": "No se puede eliminar la sesión mientras el pago está siendo procesado."}), 409

        if row["zoom_meeting_id"]:
            token = zoom.get_access_token(body.credentials)
            zoom.delete_meeting(token, row["zoom_meeting_id"])

        db.delete_appointment(row["session_id"], db_path=db_path)
        return jsonify({"deleted": True, "session_id": row["session_id"]}), 200

    return app
