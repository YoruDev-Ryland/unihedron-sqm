"""Build a local-time, one-column-per-night annual SQM heatmap."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database

SLOT_MINUTES = 30
SLOT_SECONDS = SLOT_MINUTES * 60
NIGHT_START_HOUR = 18
NIGHT_END_HOUR = 7
SLOTS_PER_NIGHT = ((24 - NIGHT_START_HOUR) + NIGHT_END_HOUR) * 2
SCALE_MIN_MPSAS = 16.0
SCALE_MAX_MPSAS = 22.5


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


def build_annual_map(db: Database, year: int, timezone_name: str) -> dict:
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
    cells: dict[tuple[int, int], tuple[float, int]] = {}
    for item in db.annual_bins(start, end, SLOT_SECONDS):
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
        prior_weight, prior_count = cells.get((day, slot), (0.0, 0))
        cells[(day, slot)] = (prior_weight + weighted_value, prior_count + count)

    output = [
        {
            "day": day,
            "slot": slot,
            "mpsas": round(weighted / count, 3),
            "count": count,
        }
        for (day, slot), (weighted, count) in sorted(cells.items())
    ]
    values = [cell["mpsas"] for cell in output]

    return {
        "year": year,
        "timezone": timezone_name,
        "days": day_count,
        "slot_minutes": SLOT_MINUTES,
        "night_start_hour": NIGHT_START_HOUR,
        "night_end_hour": NIGHT_END_HOUR,
        "slots": SLOTS_PER_NIGHT,
        "scale_min_mpsas": SCALE_MIN_MPSAS,
        "scale_max_mpsas": SCALE_MAX_MPSAS,
        "observed_nights": len({cell["day"] for cell in output}),
        "reading_count": sum(cell["count"] for cell in output),
        "min_mpsas": min(values) if values else None,
        "max_mpsas": max(values) if values else None,
        "cells": output,
    }

