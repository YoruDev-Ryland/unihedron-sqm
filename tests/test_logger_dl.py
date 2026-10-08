import asyncio
import contextlib

import pytest

from sqm_service import logger_dl
from sqm_service.db import Database
from sqm_service.sqm_client import SQMError

RECORDS = [
    "L4,25-01-06 2 03:00:00,21.04, 008.5C,234,1",
    "L4,25-01-06 2 03:05:00,21.10,-002.0C,233,1",
    "L4,55-55-55 5 55:55:55,00.00,-873.4C,255",
]


class FakeLogger:
    def __init__(self, records=RECORDS):
        self.records = records
        self.sent = []

    async def send(self, command, match, timeout):
        self.sent.append(command)
        if command == b"L1x":
            line = f"L1,{len(self.records):010d}"
        elif command.startswith(b"L4") and command.endswith(b"x"):
            line = self.records[int(command[2:-1])]
        else:
            raise SQMError("no reply")
        assert match(line)
        return line


def test_parse_pointer_and_records():
    assert logger_dl.parse_pointer("L1,0000000123") == 123
    row = logger_dl.parse_record(RECORDS[0])
    assert row[0] == 1736132400.0  # 2025-01-06T03:00:00Z
    assert row[1] == 21.04 and row[5] == 8.5
    assert logger_dl.parse_record(RECORDS[1])[5] == -2.0
    assert logger_dl.parse_record(RECORDS[2]) is None
    with pytest.raises(SQMError):
        logger_dl.parse_record("L4,garbage")


def test_download_imports_records_and_never_erases(tmp_path):
    fake = FakeLogger()

    @contextlib.asynccontextmanager
    async def session():
        yield fake

    db = Database(tmp_path / "sqm.db")
    job = logger_dl.LoggerDownload()
    asyncio.run(job.run(session, db, "4171"))
    assert job.state == {"running": False, "total": 3, "read": 3, "imported": 2, "duplicates": 0, "error": None}
    assert b"L2x" not in fake.sent
    assert db.count() == 2
    assert db.recent_imports()[0]["filename"] == "LU-DL download (serial 4171)"


def test_download_error_is_recorded(tmp_path):
    @contextlib.asynccontextmanager
    async def broken():
        raise SQMError("serial meter /dev/sqm failed")
        yield

    job = logger_dl.LoggerDownload()
    asyncio.run(job.run(broken, Database(tmp_path / "sqm.db"), "4171"))
    assert job.state["running"] is False and "failed" in job.state["error"]
