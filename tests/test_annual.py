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
    # Morning twilight up to 09:00 also belongs to the previous evening.
    database.insert_reading(
        reading(14.0), local_timestamp(2025, 1, 2, 8, 40)
    )
    # Midday falls outside the 16:00 to 09:00 window.
    database.insert_reading(
        reading(0.0), local_timestamp(2025, 1, 2, 12, 0)
    )
    # Readings below 10 mag/arcsec² (daylight) are never loaded.
    database.insert_reading(
        reading(0.0), local_timestamp(2025, 1, 2, 16, 10)
    )
    database.insert_reading(
        reading(9.9), local_timestamp(2025, 1, 2, 16, 20)
    )
    # Exactly 10 is kept; it is drawn fully transparent by the client.
    database.insert_reading(
        reading(10.0), local_timestamp(2025, 1, 2, 16, 40)
    )
    database.insert_reading(
        reading(19.0), local_timestamp(2025, 1, 2, 18, 10)
    )

    result = build_annual_map(database, 2025, "America/Chicago")
    cells = {(cell["day"], cell["slot"]): cell for cell in result["cells"]}

    assert result["days"] == 365
    assert result["slots"] == 34
    assert (result["night_start_hour"], result["night_end_hour"]) == (16, 9)
    assert (result["scale_min_mpsas"], result["scale_max_mpsas"]) == (16.0, 22.0)
    assert (result["fade_min_mpsas"], result["fade_max_mpsas"]) == (10.0, 17.0)
    assert result["observed_nights"] == 2
    assert cells[(0, 4)]["mpsas"] == 19.0
    assert cells[(0, 4)]["count"] == 2
    assert cells[(0, 16)]["mpsas"] == 21.5
    assert cells[(0, 33)]["mpsas"] == 14.0
    assert (1, 0) not in cells
    assert cells[(1, 1)]["mpsas"] == 10.0
    assert cells[(1, 4)]["mpsas"] == 19.0
    assert len(cells) == 5
    database.close()


def test_leap_year_has_366_columns(tmp_path: Path):
    database = Database(tmp_path / "sqm.db")
    result = build_annual_map(database, 2024, "UTC")
    assert result["days"] == 366
    assert result["cells"] == []
    database.close()



from sqm_service.annual import clear_cache, darkness_window

DENVER = (39.74, -104.99)


def denver_timestamp(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo("America/Denver")
    ).timestamp()


def test_darkness_window_in_winter():
    window = darkness_window(2025, 354, *DENVER, "America/Denver")  # Dec 21
    # Astronomical dusk about 18:12, dawn about 05:48 local.
    assert 120 <= window["dusk"] <= 150     # 18:00 to 18:30 from 16:00
    assert 805 <= window["dawn"] <= 840     # 05:25 to 06:00 from 16:00


def test_no_astronomical_darkness_in_northern_summer():
    assert darkness_window(2025, 171, 57.0, -3.0, "Europe/London") is None  # Jun 21


def test_site_adds_darkness_and_moon_flags(tmp_path: Path):
    clear_cache()
    database = Database(tmp_path / "sqm.db")
    # Full moon night of 2025-01-13; a reading at 23:00 local in Denver.
    database.insert_reading(reading(18.5), denver_timestamp(2025, 1, 13, 23, 0))
    plain = build_annual_map(database, 2025, "America/Denver")
    assert "darkness" not in plain and "moon" not in plain["cells"][0]
    sited = build_annual_map(database, 2025, "America/Denver", site=DENVER)
    assert len(sited["darkness"]) == 365
    cell = sited["cells"][0]
    assert cell["moon"] is True and cell["moon_fraction"] > 0.95
    database.close()
