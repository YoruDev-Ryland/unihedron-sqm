"""Prometheus text exposition (format 0.0.4)."""

from __future__ import annotations

from . import sky

HELP = {
    "sqm_mpsas": "Sky brightness in magnitudes per square arcsecond",
    "sqm_temperature_celsius": "Meter sensor temperature",
    "sqm_frequency_hertz": "Light sensor frequency",
    "sqm_nelm": "Naked-eye limiting magnitude",
    "sqm_bortle_class": "Bortle dark-sky class",
    "sqm_last_success_timestamp_seconds": "Unix time of the last successful reading",
    "sqm_collector_up": "1 while readings are arriving",
    "sqm_readings_stored": "Readings stored in the database",
}


def _number(value: float) -> str:
    # Full precision: `:g` would round a Unix timestamp to six digits.
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def render(latest: dict | None, serial: str, collector_up: bool, last_success: float | None, stored: int) -> str:
    values: dict[str, float | None] = dict.fromkeys(HELP)
    if latest:
        described = sky.describe(latest["mpsas"])
        values.update(
            sqm_mpsas=latest["mpsas"],
            sqm_temperature_celsius=latest["temperature_c"],
            sqm_frequency_hertz=latest["frequency_hz"],
            sqm_nelm=described["nelm"],
            sqm_bortle_class=described["bortle"],
        )
    values.update(
        sqm_last_success_timestamp_seconds=last_success,
        sqm_collector_up=1 if collector_up else 0,
        sqm_readings_stored=stored,
    )
    lines = []
    for name, value in values.items():
        if value is None:
            continue
        lines += [f"# HELP {name} {HELP[name]}", f"# TYPE {name} gauge", f'{name}{{serial="{serial}"}} {_number(value)}']
    return "\n".join(lines) + "\n"
