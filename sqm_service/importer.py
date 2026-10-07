"""Parser for Unihedron Device Manager SDF 1.0 .dat and plain .csv logs."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .db import Database


@dataclass
class ImportResult:
    filename: str
    parsed: int = 0
    imported: int = 0
    duplicates: int = 0
    bad_lines: int = 0
    bad_samples: list[str] = field(default_factory=list)
    first_timestamp: float | None = None
    last_timestamp: float | None = None

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "parsed": self.parsed,
            "imported": self.imported,
            "duplicates": self.duplicates,
            "bad_lines": self.bad_lines,
            "bad_samples": self.bad_samples,
            "first_timestamp": self.first_timestamp,
            "last_timestamp": self.last_timestamp,
        }


def parse_utc(value: str) -> float:
    value = value.strip().removesuffix("Z")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def import_text(db: Database, filename: str, content: bytes) -> ImportResult:
    result = ImportResult(filename=filename)
    rows: list[tuple] = []
    handle = io.StringIO(content.decode("utf-8-sig", errors="replace"))

    for line_number, source_line in enumerate(handle, start=1):
        line = source_line.strip()
        if not line or line.startswith("#"):
            continue
        separator = ";" if ";" in line else ","
        parts = [part.strip() for part in line.split(separator)]
        if parts and (
            "utc date" in parts[0].lower()
            or parts[0].lower() in {"timestamp", "datetime", "utc"}
        ):
            continue
        if len(parts) != 6:
            result.bad_lines += 1
            if len(result.bad_samples) < 5:
                result.bad_samples.append(f"line {line_number}: expected 6 fields")
            continue
        try:
            timestamp = parse_utc(parts[0])
            temperature = float(parts[2])
            counts = int(parts[3])
            frequency = int(parts[4])
            mpsas = float(parts[5])
        except (ValueError, OverflowError) as exc:
            result.bad_lines += 1
            if len(result.bad_samples) < 5:
                result.bad_samples.append(f"line {line_number}: {exc}")
            continue

        rows.append(
            (
                timestamp,
                mpsas,
                frequency,
                counts,
                0.0,
                temperature,
                line,
            )
        )
        result.parsed += 1
        result.first_timestamp = (
            timestamp
            if result.first_timestamp is None
            else min(result.first_timestamp, timestamp)
        )
        result.last_timestamp = (
            timestamp
            if result.last_timestamp is None
            else max(result.last_timestamp, timestamp)
        )

    result.imported, result.duplicates = db.import_readings(rows)
    db.record_import(
        result.filename, result.imported, result.duplicates, result.bad_lines
    )
    return result

