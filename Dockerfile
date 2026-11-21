FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/appointments.db

COPY pyproject.toml ./
RUN uv sync --no-dev --no-install-project

COPY app ./app
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uv", "run", "--no-dev", "gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "app:create_app()"]
