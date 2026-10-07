from __future__ import annotations

import asyncio

from fake_serial import FakeSerialSQM
from sqm_service.sqm_client import (
    SQMError,
    discover_serial_devices,
    get_serial_info,
    get_serial_reading,
)


def test_serial_identity_and_reading_round_trip():
    with FakeSerialSQM() as meter:
        info = asyncio.run(get_serial_info(meter.path, 115200, 1))
        reading = asyncio.run(get_serial_reading(meter.path, 115200, 1))

    assert info["model"] == 3
    assert info["serial"] == 1234
    assert reading["mpsas"] == 21.37
    assert reading["temperature_c"] == 11.8
    assert meter.commands == [b"ix", b"rx"]


def test_serial_discovery_verifies_sqms():
    with FakeSerialSQM() as meter:
        devices = asyncio.run(
            discover_serial_devices(paths=[meter.path], timeout=1)
        )

    matched = [device for device in devices if device["path"] == meter.path]
    assert len(matched) == 1
    assert matched[0]["transport"] == "serial"
    assert matched[0]["verified"] is True
    assert matched[0]["device"]["serial"] == 1234


def test_serial_timeout_is_reported_as_sqm_error():
    with FakeSerialSQM(responses={b"other": b"unused\r\n"}) as meter:
        try:
            asyncio.run(get_serial_info(meter.path, 115200, 0.1))
        except SQMError as exc:
            assert "did not reply" in str(exc)
        else:
            raise AssertionError("silent serial meter was accepted")
