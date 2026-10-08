"""Download records stored in an SQM-LU-DL's internal logger (experimental).

Commands follow the SQM-LU-DL Operator's Manual revision 20260924, §8.5:
`L1x` reports how many records are stored, and `L4<10-digit pointer>x`
returns one record, `L4,YY-MM-DD d HH:MM:SS,<mpsas>,<temp>C,<battery>,...`,
with pointers starting at 0 and the logger clock in UTC. Erased slots answer
with a `55-55-55` date. The erase command `L2x` is never sent.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .sqm_client import SQMError

CHUNK = 200
TIMEOUT = 3.0
POINTER = re.compile(r"^L1,(\d+)$")
RECORD = re.compile(
    r"^L4,(\d{2})-(\d{2})-(\d{2}) \d (\d{2}):(\d{2}):(\d{2}),\s*(-?\d+\.\d+),\s*(-?\d+\.\d)C"
)


def parse_pointer(line: str) -> int:
    found = POINTER.match(line.strip())
    if not found:
        raise SQMError(f"unexpected logger pointer response: {line!r}")
    return int(found.group(1))


def parse_record(line: str) -> tuple | None:
    text = line.strip()
    if text.startswith("L4,55-55-55"):
        return None
    found = RECORD.match(text)
    if not found:
        raise SQMError(f"unexpected logger record: {line!r}")
    yy, mo, dd, hh, mi, ss, mpsas, temperature = found.groups()
    stamp = datetime(2000 + int(yy), int(mo), int(dd), int(hh), int(mi), int(ss), tzinfo=timezone.utc)
    return (stamp.timestamp(), float(mpsas), 0, 0, 0.0, float(temperature), text)


class LoggerDownload:
    def __init__(self) -> None:
        self.state = {"running": False, "total": 0, "read": 0, "imported": 0, "duplicates": 0, "error": None}

    async def run(self, session_factory, db, serial: str) -> None:
        self.state = {"running": True, "total": 0, "read": 0, "imported": 0, "duplicates": 0, "error": None}
        rows: list[tuple] = []
        try:
            async with session_factory() as link:
                total = parse_pointer(await link.send(b"L1x", lambda l: l.startswith("L1,"), TIMEOUT))
            self.state["total"] = total
            for start in range(0, total, CHUNK):
                # Release the connection between chunks so collection continues.
                async with session_factory() as link:
                    for pointer in range(start, min(start + CHUNK, total)):
                        line = await link.send(b"L4%010dx" % pointer, lambda l: l.startswith("L4,"), TIMEOUT)
                        row = parse_record(line)
                        if row is not None:
                            rows.append(row)
                        self.state["read"] = pointer + 1
            imported, duplicates = db.import_readings(rows)
            db.record_import(f"LU-DL download (serial {serial})", imported, duplicates, 0)
            self.state.update(imported=imported, duplicates=duplicates)
        except SQMError as exc:
            self.state["error"] = str(exc)
        finally:
            self.state["running"] = False
