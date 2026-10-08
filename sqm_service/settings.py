"""Typed application settings: defaults, environment overrides, and storage.

Values set in the environment win and lock the field in the web interface.
Others are stored in the `app_settings` table under `cfg.<section>.<field>`.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import Database

SECTIONS = ("site", "mqtt", "prometheus", "alerts")
TRUE = {"1", "true", "yes", "on"}
FALSE = {"0", "false", "no", "off", ""}


class SettingsError(ValueError):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class SettingLocked(SettingsError):
    pass


@dataclass(frozen=True)
class Field:
    section: str
    name: str
    kind: type
    default: Any
    secret: bool = False
    optional: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None

    @property
    def key(self) -> str:
        return f"cfg.{self.section}.{self.name}"

    @property
    def env(self) -> str:
        return f"SQM_{self.section.upper()}_{self.name.upper()}"


FIELDS = (
    Field("site", "latitude", float, None, optional=True, minimum=-90, maximum=90),
    Field("site", "longitude", float, None, optional=True, minimum=-180, maximum=180),
    Field("mqtt", "host", str, ""),
    Field("mqtt", "port", int, 1883, minimum=1, maximum=65535),
    Field("mqtt", "username", str, ""),
    Field("mqtt", "password", str, "", secret=True),
    Field("mqtt", "tls", bool, False),
    Field("mqtt", "base_topic", str, "sqm"),
    Field("mqtt", "discovery_prefix", str, "homeassistant"),
    Field("mqtt", "discovery", bool, True),
    Field("prometheus", "public", bool, False),
    Field("alerts", "url", str, ""),
    Field("alerts", "format", str, "json", choices=("json", "ntfy")),
    Field("alerts", "offline_hours", float, 2.0, minimum=0.25, maximum=168),
)
BY_KEY = {(field.section, field.name): field for field in FIELDS}


def coerce(field: Field, raw: Any) -> Any:
    """Turn form or environment input into the field's type, or raise."""
    if field.kind is bool:
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in TRUE:
            return True
        if text in FALSE:
            return False
        raise SettingsError(field.name, f"{field.name} must be true or false")
    text = "" if raw is None else str(raw).strip()
    if field.kind is str:
        if field.choices and text not in field.choices:
            raise SettingsError(field.name, f"{field.name} must be one of {', '.join(field.choices)}")
        return text
    if text == "":
        if field.optional:
            return None
        raise SettingsError(field.name, f"{field.name} is required")
    try:
        value = field.kind(float(text)) if field.kind is int else field.kind(text)
    except ValueError as exc:
        raise SettingsError(field.name, f"{field.name} must be a number") from exc
    if field.minimum is not None and value < field.minimum:
        raise SettingsError(field.name, f"{field.name} must be at least {field.minimum:g}")
    if field.maximum is not None and value > field.maximum:
        raise SettingsError(field.name, f"{field.name} must be at most {field.maximum:g}")
    return value


class Settings:
    def __init__(self, db: Database, environ: Mapping[str, str] = os.environ) -> None:
        self._db = db
        self._environ = environ
        self._callbacks: dict[str, list[Callable[[dict], None]]] = {}

    def _from_env(self, field: Field) -> str | None:
        value = self._environ.get(field.env)
        if value:
            return value
        path = self._environ.get(f"{field.env}_FILE") if field.secret else None
        if path:
            return Path(path).read_text(encoding="utf-8").strip()
        return None

    def locked(self, section: str, name: str) -> bool:
        return self._from_env(BY_KEY[(section, name)]) is not None

    def get(self, section: str, name: str) -> Any:
        field = BY_KEY[(section, name)]
        env = self._from_env(field)
        if env is not None:
            return coerce(field, env)
        stored = self._db.get_setting(field.key)
        return field.default if stored is None else coerce(field, stored)

    def section(self, section: str) -> dict[str, Any]:
        return {f.name: self.get(section, f.name) for f in FIELDS if f.section == section}

    def public(self) -> dict[str, dict[str, dict]]:
        result: dict[str, dict[str, dict]] = {s: {} for s in SECTIONS}
        for field in FIELDS:
            locked = self.locked(field.section, field.name)
            value = self.get(field.section, field.name)
            result[field.section][field.name] = (
                {"set": bool(value), "locked": locked}
                if field.secret
                else {"value": value, "locked": locked}
            )
        return result

    def update(self, section: str, values: dict) -> dict:
        if section not in SECTIONS:
            raise SettingsError("section", f"unknown settings section {section!r}")
        staged: dict[Field, Any] = {}
        for name, raw in values.items():
            field = BY_KEY.get((section, name))
            if field is None:
                raise SettingsError(name, f"unknown setting {name!r}")
            if self.locked(section, name):
                raise SettingLocked(name, f"{name} is set by the environment")
            staged[field] = coerce(field, raw)
        for field, value in staged.items():
            stored = "" if value is None else str(value).lower() if field.kind is bool else str(value)
            self._db.set_setting(field.key, stored)
        current = self.section(section)
        for callback in self._callbacks.get(section, []):
            callback(current)
        return self.public()[section]

    def on_change(self, section: str, callback: Callable[[dict], None]) -> None:
        self._callbacks.setdefault(section, []).append(callback)
