"""Build a local-time, one-column-per-night annual SQM heatmap."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import astro
from .db import Database

SLOT_MINUTES = 30
SLOT_SECONDS = SLOT_MINUTES * 60
# A wide window from 16:00 to 09:00 keeps each night centred in its column,
# with dusk and dawn visible above and below it.
NIGHT_START_HOUR = 16
NIGHT_END_HOUR = 9
SLOTS_PER_NIGHT = ((24 - NIGHT_START_HOUR) + NIGHT_END_HOUR) * 2
SCALE_MIN_MPSAS = 16.0
SCALE_MAX_MPSAS = 22.0
# Cells fade in from fully transparent at 10 mag/arcsec² to fully opaque at
# 17, so twilight blends into the night. Readings below 10 (daylight) are not
# loaded at all.
FADE_MIN_MPSAS = 10.0
FADE_MAX_MPSAS = 17.0
DARK_SUN_ALTITUDE = -18.0
WINDOW_MINUTES = SLOTS_PER_NIGHT * SLOT_MINUTES
SAMPLE_MINUTES = 5


def timezone_for(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown TZ timezone: {name}") from exc


def available_years(db: Database, timezone_name: str, fallback_year: int) -> list[int]:
    first, last = db.reading_bounds()
    if first is None or last is None:
        return [fallback_year]
    timezone = timezone_for(timezone_name)
    first_year = datetime.fromtimestamp(first, timezone).year
    last_year = datetime.fromtimestamp(last, timezone).year
    return list(range(last_year, first_year - 1, -1))


def darkness_window(year: int, day: int, lat: float, lon: float, timezone_name: str) -> dict | None:
    """Minutes after 16:00 local when astronomical darkness starts and ends."""
    zone = timezone_for(timezone_name)
    night = date(year, 1, 1) + timedelta(days=day)
    start = datetime(night.year, night.month, night.day, NIGHT_START_HOUR, tzinfo=zone).timestamp()
    samples = [
        astro.sun_altitude(start + minute * 60, lat, lon) < DARK_SUN_ALTITUDE
        for minute in range(0, WINDOW_MINUTES + 1, SAMPLE_MINUTES)
    ]
    if not any(samples):
        return None
    first = samples.index(True)
    last = len(samples) - 1 - samples[::-1].index(True)
    return {"dusk": first * SAMPLE_MINUTES, "dawn": last * SAMPLE_MINUTES}


@lru_cache(maxsize=8)
def _darkness_year(year: int, lat: float, lon: float, timezone_name: str, days: int) -> tuple:
    return tuple(darkness_window(year, day, lat, lon, timezone_name) for day in range(days))


def clear_cache() -> None:
    _darkness_year.cache_clear()


def build_annual_map(
    db: Database,
    year: int,
    timezone_name: str,
    site: tuple[float, float] | None = None,
) -> dict:
    timezone = timezone_for(timezone_name)
    first_day = date(year, 1, 1)
    next_year = date(year + 1, 1, 1)
    day_count = (next_year - first_day).days

    # One column is the observing night beginning on that date. The final
    # column therefore extends into the morning of January 1 of the next year.
    start_local = datetime(
        year, 1, 1, NIGHT_START_HOUR, tzinfo=timezone
    )
    end_local = datetime(
        year + 1, 1, 1, NIGHT_END_HOUR, tzinfo=timezone
    )
    start = start_local.timestamp()
    end = end_local.timestamp()

    # During the fall DST fold, two UTC bins can map to one local cell. Keep a
    # weighted aggregate so both repeated half-hours are represented.
    cells: dict[tuple[int, int], tuple[float, int, float]] = {}
    for item in db.annual_bins(start, end, SLOT_SECONDS, minimum_mpsas=FADE_MIN_MPSAS):
        bin_number = int(item["bin"])
        midpoint = start + bin_number * SLOT_SECONDS + SLOT_SECONDS / 2
        local = datetime.fromtimestamp(midpoint, timezone)
        local_minutes = local.hour * 60 + local.minute

        if local_minutes >= NIGHT_START_HOUR * 60:
            night_date = local.date()
            slot = (local_minutes - NIGHT_START_HOUR * 60) // SLOT_MINUTES
        elif local_minutes < NIGHT_END_HOUR * 60:
            night_date = local.date() - timedelta(days=1)
            slot = (
                (24 - NIGHT_START_HOUR) * 60 + local_minutes
            ) // SLOT_MINUTES
        else:
            continue

        day = (night_date - first_day).days
        if not (0 <= day < day_count and 0 <= slot < SLOTS_PER_NIGHT):
            continue

        count = int(item["reading_count"])
        weighted_value = float(item["avg_mpsas"]) * count
        prior_weight, prior_count, first_midpoint = cells.get((day, slot), (0.0, 0, midpoint))
        cells[(day, slot)] = (prior_weight + weighted_value, prior_count + count, first_midpoint)

    output = [
        {
            "day": day,
            "slot": slot,
            "mpsas": round(weighted / count, 3),
            "count": count,
        }
        for (day, slot), (weighted, count, _midpoint) in sorted(cells.items())
    ]
    midpoints = [midpoint for _key, (_weighted, _count, midpoint) in sorted(cells.items())]
    values = [cell["mpsas"] for cell in output]

    result = {
        "year": year,
        "timezone": timezone_name,
        "days": day_count,
        "slot_minutes": SLOT_MINUTES,
        "night_start_hour": NIGHT_START_HOUR,
        "night_end_hour": NIGHT_END_HOUR,
        "slots": SLOTS_PER_NIGHT,
        "scale_min_mpsas": SCALE_MIN_MPSAS,
        "scale_max_mpsas": SCALE_MAX_MPSAS,
        "fade_min_mpsas": FADE_MIN_MPSAS,
        "fade_max_mpsas": FADE_MAX_MPSAS,
        "observed_nights": len({cell["day"] for cell in output}),
        "reading_count": sum(cell["count"] for cell in output),
        "min_mpsas": min(values) if values else None,
        "max_mpsas": max(values) if values else None,
        "cells": output,
    }
    if site is not None:
        lat, lon = site
        result["darkness"] = list(_darkness_year(year, lat, lon, timezone_name, day_count))
        for cell, midpoint in zip(output, midpoints):
            if astro.moon_altitude(midpoint, lat, lon) > 0:
                cell["moon"] = True
                cell["moon_fraction"] = round(astro.moon_phase(midpoint)[0], 2)
    return result

