"""Runtime configuration sourced from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def _text(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_text(name) or str(default))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(_text(name) or str(default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    value = _text(name, "true" if default else "false").lower()
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"1", "true", "yes", "on"}


def read_secret(value_name: str, file_name: str) -> str:
    path = _text(file_name)
    if path:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"could not read {file_name}={path}: {exc}") from exc
    return _text(value_name)


class Config:
    def __init__(self) -> None:
        self.data_dir = Path(_text("DATA_DIR", "/data"))
        self.db_path = Path(_text("DB_PATH", str(self.data_dir / "sqm.db")))
        self.import_dir = Path(_text("IMPORT_DIR", "/imports"))
        self.timezone = _text("TZ", "UTC")

        self.web_port = _integer("WEB_PORT", 7942, 1, 65535)
        self.public_url = _text("PUBLIC_URL", f"http://localhost:{self.web_port}")
        self.cookie_secure = _boolean("SESSION_COOKIE_SECURE", False)
        self.session_hours = _integer("SESSION_HOURS", 24, 1, 24 * 30)

        self.sqm_transport = (_text("SQM_TRANSPORT") or "ethernet").lower()
        if self.sqm_transport not in {"ethernet", "serial"}:
            raise ValueError("SQM_TRANSPORT must be ethernet or serial")

        # Blank endpoints are intentional: first-time users select a meter in the UI.
        self.sqm_host: str | None = _text("SQM_HOST") or None
        self.sqm_port = _integer("SQM_PORT", 10001, 1, 65535)
        self.sqm_serial_device: str | None = _text("SQM_SERIAL_DEVICE") or None
        self.sqm_baud_rate = _integer("SQM_BAUD_RATE", 115200, 1200, 3_000_000)
        self.poll_interval = _number("SQM_POLL_INTERVAL", 60, 5, 86400)
        self.connect_timeout = _number("SQM_CONNECT_TIMEOUT", 5, 0.25, 120)
        self.read_timeout = _number("SQM_READ_TIMEOUT", 120, 1, 300)
        self.retention_days = _integer("SQM_RETENTION_DAYS", 0, 0, 36500)

        self.admin_username = _text("ADMIN_USERNAME", "admin")
        self.admin_password = read_secret("ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE")
        self.api_key = read_secret("API_KEY", "API_KEY_FILE")

        self.max_upload_mb = _integer("MAX_UPLOAD_MB", 20, 1, 250)
        self.cors_origins = [
            item.strip()
            for item in _text("CORS_ORIGINS", "").split(",")
            if item.strip()
        ]

