from pathlib import Path

from sqm_service.db import Database
from sqm_service.importer import import_text


SAMPLE = b"""# Light Pollution Monitoring Data Format 1.0
# END OF HEADER
2024-01-01T18:00:00.083;2024-01-01T12:00:00.083;24.4;0;556911;0.00
2024-01-01T18:01:00.084;2024-01-01T12:01:00.084;24.8;0;557063;0.00
bad row
"""


def test_import_and_deduplicate(tmp_path: Path):
    database = Database(tmp_path / "sqm.db")
    first = import_text(database, "sample.dat", SAMPLE)
    assert first.parsed == 2
    assert first.imported == 2
    assert first.duplicates == 0
    assert first.bad_lines == 1
    assert database.count() == 2

    second = import_text(database, "sample.dat", SAMPLE)
    assert second.imported == 0
    assert second.duplicates == 2
    assert database.count() == 2
    database.close()


def test_comma_separated_log(tmp_path: Path):
    database = Database(tmp_path / "sqm.db")
    content = (
        b"2024-01-01T18:00:00.083,2024-01-01T12:00:00.083,"
        b"24.4,0,556911,18.22\n"
    )
    result = import_text(database, "sample.csv", content)
    assert result.imported == 1
    assert database.latest_reading()["mpsas"] == 18.22
    database.close()

