from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqm_service.annual import build_annual_map
from sqm_service.db import Database


def reading(value: float) -> dict:
    return {
        "mpsas": value,
        "frequency_hz": 10,
        "period_counts": 0,
        "period_seconds": 0.0,
        "temperature_c": 10.0,
        "raw": f"test {value}",
    }


def local_timestamp(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo("America/Chicago")
    ).timestamp()


def test_annual_map_groups_complete_observing_nights(tmp_path: Path):
    database = Database(tmp_path / "sqm.db")
    database.insert_reading(
        reading(18.0), local_timestamp(2025, 1, 1, 18, 5)
    )
    database.insert_reading(
        reading(20.0), local_timestamp(2025, 1, 1, 18, 20)
    )
    # After midnight remains in the January 1 observing-night column.
    database.insert_reading(
        reading(21.5), local_timestamp(2025, 1, 2, 0, 10)
    )
    # Daylight/invalid zero values are omitted.
    database.insert_reading(
        reading(0.0), local_timestamp(2025, 1, 2, 12, 0)
    )
    database.insert_reading(
        reading(19.0), local_timestamp(2025, 1, 2, 18, 10)
    )

    result = build_annual_map(database, 2025, "America/Chicago")
    cells = {(cell["day"], cell["slot"]): cell for cell in result["cells"]}

    assert result["days"] == 365
    assert result["observed_nights"] == 2
    assert cells[(0, 0)]["mpsas"] == 19.0
    assert cells[(0, 0)]["count"] == 2
    assert cells[(0, 12)]["mpsas"] == 21.5
    assert cells[(1, 0)]["mpsas"] == 19.0
    assert len(cells) == 3
    database.close()


def test_leap_year_has_366_columns(tmp_path: Path):
    database = Database(tmp_path / "sqm.db")
    result = build_annual_map(database, 2024, "UTC")
    assert result["days"] == 366
    assert result["cells"] == []
    database.close()

