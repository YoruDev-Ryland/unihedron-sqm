from sqm_service import exporter
from sqm_service.db import Database
from sqm_service.importer import import_text


def reading(mpsas):
    return {"mpsas": mpsas, "frequency_hz": 3, "period_counts": 127534,
            "period_seconds": 0.276, "temperature_c": 8.4, "raw": "r"}


def fill(db):
    for index in range(7000):  # more than one 5,000-row page
        db.insert_reading(reading(20 + (index % 10) / 10), 1_760_000_000 + index * 60)


def test_csv_has_header_and_every_row(tmp_path):
    db = Database(tmp_path / "a.db")
    fill(db)
    lines = "".join(exporter.stream(db, "csv", None, None, "UTC", 4171)).splitlines()
    assert lines[0] == "utc_iso,local_iso,mpsas,temperature_c,frequency_hz,period_counts,period_seconds"
    assert len(lines) == 7001
    assert lines[1].startswith("2025-10-09T08:53:20.000Z,2025-10-09T08:53:20.000,20.0,8.4,3,127534,0.276")


def test_dat_round_trips_through_the_importer(tmp_path):
    source = Database(tmp_path / "a.db")
    fill(source)
    text = "".join(exporter.stream(source, "dat", None, None, "America/Denver", 4171))
    assert text.startswith("# Community Standard Skyglow Data Format 1.0")
    target = Database(tmp_path / "b.db")
    first = import_text(target, "export.dat", text.encode())
    assert (first.parsed, first.imported, first.bad_lines) == (7000, 7000, 0)
    again = import_text(target, "export.dat", text.encode())
    assert again.imported == 0 and again.duplicates == 7000


def test_range_is_respected(tmp_path):
    db = Database(tmp_path / "a.db")
    fill(db)
    since = 1_760_000_000 + 100 * 60
    until = 1_760_000_000 + 199 * 60
    lines = "".join(exporter.stream(db, "csv", since, until, "UTC", None)).splitlines()
    assert len(lines) == 101


def test_empty_range_gives_header_only(tmp_path):
    db = Database(tmp_path / "a.db")
    text = "".join(exporter.stream(db, "dat", None, None, "UTC", None))
    assert all(line.startswith("#") for line in text.splitlines())


def test_filename():
    assert exporter.filename("csv", 4171, 1_760_000_000, 1_760_420_000, "UTC") == "sqm-4171-20251009-20251014.csv"
    assert exporter.filename("dat", None, None, None, "UTC") == "sqm-meter-empty.dat"


def test_dat_export_reimports_into_the_same_database_without_duplicates(tmp_path):
    db = Database(tmp_path / "a.db")
    # Collected readings carry sub-millisecond time.time() precision.
    for index in range(5):
        db.insert_reading(reading(21.0), 1_760_000_000.123456 + index * 60.000789)
    text = "".join(exporter.stream(db, "dat", None, None, "UTC", 4171))
    result = import_text(db, "export.dat", text.encode())
    assert (result.imported, result.duplicates) == (0, 5)
    assert db.count() == 5
