"""Stream stored readings as CSV or as an SDF 1.0 `.dat` log.

The `.dat` layout matches what `importer.py` reads, so an export can be
imported again (or into Unihedron's own tools) without changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .db import Database

PAGE = 5000
CSV_HEADER = "utc_iso,local_iso,mpsas,temperature_c,frequency_hz,period_counts,period_seconds\n"


def _stamp(ts: float, zone) -> str:
    return datetime.fromtimestamp(ts, zone).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


def _dat_header(zone_name: str, serial: int | None) -> str:
    lines = [
        "Community Standard Skyglow Data Format 1.0",
        "URL: http://www.darksky.org/measurements",
        "Number of header lines: 9",
        f"Instrument ID: SQM {serial if serial is not None else 'unknown'}",
        "Data supplier: Unihedron SQM Collector export",
        f"Local timezone: {zone_name}",
        "UTC Date & Time, Local Date & Time, Temperature, Counts, Frequency, MSAS",
        "YYYY-MM-DDTHH:mm:ss.fff;YYYY-MM-DDTHH:mm:ss.fff;Celsius;number;Hz;mag/arcsec^2",
        "END OF HEADER",
    ]
    return "".join(f"# {line}\n" for line in lines)


def stream(
    db: Database,
    fmt: str,
    since: float | None,
    until: float | None,
    timezone_name: str,
    serial: int | None,
) -> Iterator[str]:
    zone = ZoneInfo(timezone_name)
    yield CSV_HEADER if fmt == "csv" else _dat_header(timezone_name, serial)
    cursor = (since - 1e-6) if since is not None else float("-inf")
    while True:
        rows = db.readings_after(cursor, until, PAGE)
        if not rows:
            return
        chunk = []
        for row in rows:
            utc = _stamp(row["ts"], timezone.utc)
            local = _stamp(row["ts"], zone)
            if fmt == "csv":
                chunk.append(
                    f"{utc}Z,{local},{row['mpsas']},{row['temperature_c']},"
                    f"{row['frequency_hz']},{row['period_counts']},{row['period_seconds']}\n"
                )
            else:
                chunk.append(
                    f"{utc};{local};{row['temperature_c']:.1f};{row['period_counts']};"
                    f"{row['frequency_hz']};{row['mpsas']:.2f}\n"
                )
        yield "".join(chunk)
        cursor = rows[-1]["ts"]


def filename(
    fmt: str, serial: int | None, first: float | None, last: float | None, timezone_name: str
) -> str:
    name = f"sqm-{serial if serial is not None else 'meter'}"
    if first is None or last is None:
        return f"{name}-empty.{fmt}"
    zone = ZoneInfo(timezone_name)
    day = lambda ts: datetime.fromtimestamp(ts, zone).strftime("%Y%m%d")  # noqa: E731
    return f"{name}-{day(first)}-{day(last)}.{fmt}"
