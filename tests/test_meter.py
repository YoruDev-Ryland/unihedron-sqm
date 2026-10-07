from __future__ import annotations

import asyncio

import pytest

from sqm_service import meter
from sqm_service.sqm_client import SQMError

# Replies in the format of an SQM-LE with feature 80. Its interval reply omits
# the "I," prefix that the manual's example shows.
REAL = {
    b"ix": "i,00000004,00000003,00000080,00004171",
    b"cx": "c,00000019.40m,0000201.330s, 021.3C,00000008.71m, 023.8C",
    b"Ix": "0000000005s,0000000005s,00000018.00m,00000250.84m",
    b"zcalDx": "zxdU",
    b"Kx": "K,CRGT",
    b"r1x": "r, 00.00m,0000555673Hz,0000000000c,0000000.000s, 056.0C, 00.00m,F",
}


class FakeMeter:
    """Answers commands like the meter, tracking interval settings."""

    def __init__(self, replies=None, locked=False, lock_rules="K,CRGT"):
        self.replies = dict(REAL if replies is None else replies)
        self.sent: list[bytes] = []
        self.eeprom = [5, 18.0]
        self.ram = [5, 250.84]
        if locked:
            self.replies[b"zcalDx"] = "zxdL"
        self.replies[b"Kx"] = lock_rules

    def _interval(self) -> str:
        return (
            f"{self.eeprom[0]:010d}s,{self.ram[0]:010d}s,"
            f"{self.eeprom[1]:011.2f}m,{self.ram[1]:011.2f}m"
        )

    async def send(self, command: bytes, match, timeout: float) -> str:
        self.sent.append(command)
        key = command
        letter, value = command[:1], command[1:-1]
        if letter in (b"p", b"P"):
            self.ram[0] = int(value)
            if letter == b"P":
                self.eeprom[0] = int(value)
            key = b"Ix"
        elif letter in (b"t", b"T"):
            self.ram[1] = float(value)
            if letter == b"T":
                self.eeprom[1] = float(value)
            key = b"Ix"
        if key == b"Ix" and b"Ix" in self.replies:
            line = self._interval()
        elif key in self.replies:
            line = self.replies[key]
        else:
            raise SQMError("no reply within 2s")
        if not match(line):
            raise SQMError(f"unexpected reply {line!r}")
        return line


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ parsers


def test_parse_interval_with_and_without_prefix():
    expected = {
        "eeprom_period_s": 5,
        "ram_period_s": 5,
        "eeprom_threshold_mpsas": 18.0,
        "ram_threshold_mpsas": 250.84,
    }
    assert meter.parse_interval(REAL[b"Ix"]) == expected
    assert meter.parse_interval("I," + REAL[b"Ix"]) == expected
    with pytest.raises(SQMError):
        meter.parse_interval("r, 00.00m,0000555673Hz")


def test_parse_calibration():
    assert meter.parse_calibration(REAL[b"cx"]) == {
        "light_offset_mpsas": 19.4,
        "dark_period_s": 201.33,
        "light_temperature_c": 21.3,
        "sensor_offset_mpsas": 8.71,
        "dark_temperature_c": 23.8,
    }


def test_parse_lock_switch_and_rules():
    assert meter.parse_lock_switch("zxdU") is False
    assert meter.parse_lock_switch("zxdL") is True
    assert meter.parse_lock_rules("K,CRGT") == {
        "calibration": True,
        "report_interval": True,
        "configuration": True,
        "lock_settings": True,
    }
    assert meter.parse_lock_rules("K,crGt")["report_interval"] is False


def test_parse_freshness():
    assert meter.parse_freshness(REAL[b"r1x"]) == "frequency"
    assert meter.parse_freshness(REAL[b"r1x"][:-1] + "P") == "period"
    assert meter.parse_freshness(REAL[b"r1x"][:-1] + "S") == "stale"


# ------------------------------------------------------------------ details


def test_details_reads_everything_from_a_healthy_meter():
    details = run(meter.read_details(FakeMeter().send))
    assert details["health"]["status"] == "ok"
    assert details["info"]["serial"] == 4171
    assert details["calibration"]["light_offset_mpsas"] == 19.4
    assert details["interval"]["enabled"] is True
    assert details["interval"]["ram_period_s"] == 5
    assert details["lock"] == {
        "locked": False,
        "rules": {
            "calibration": True,
            "report_interval": True,
            "configuration": True,
            "lock_settings": True,
        },
    }
    assert details["freshness"] == "frequency"
    assert details["errors"] == {}


def test_details_skip_commands_older_firmware_lacks():
    fake = FakeMeter()
    fake.replies[b"ix"] = "i,00000004,00000003,00000040,00004171"
    details = run(meter.read_details(fake.send))
    assert b"Kx" not in fake.sent  # feature 46+
    assert b"r1x" not in fake.sent  # feature 58+
    assert details["lock"]["rules"] is None
    assert details["freshness"] is None


def test_details_report_meter_that_does_not_answer_commands():
    async def send(command, match, timeout):
        raise SQMError("no reply within 7s")

    async def identify():
        return {
            "protocol": None,
            "model": None,
            "feature": None,
            "serial": 4171,
            "raw": "r, 00.00m,0000555702Hz,0000000000c,0000000.000s, 055.7C,00004171",
            "interval_reporting": True,
        }

    details = run(meter.read_details(send, identify=identify))
    assert details["health"]["status"] == "commands_unanswered"
    assert "flow control" in details["health"]["message"]
    assert details["info"]["serial"] == 4171
    assert details["calibration"] is None


def test_details_report_unreachable_meter():
    async def send(command, match, timeout):
        raise SQMError("connect to 192.0.2.9:10001 failed: timed out")

    async def identify():
        raise SQMError("connect to 192.0.2.9:10001 failed: timed out")

    details = run(meter.read_details(send, identify=identify))
    assert details["health"]["status"] == "unreachable"
    assert "timed out" in details["health"]["message"]


# --------------------------------------------------------------- interval


def test_turning_interval_off_uses_ram_then_eeprom_and_verifies():
    fake = FakeMeter()
    result = run(meter.set_interval(fake.send, period_s=0, permanent=True))
    assert fake.sent[-3:] == [b"p0000000000x", b"P0000000000x", b"Ix"]
    assert result["eeprom_period_s"] == 0 and result["ram_period_s"] == 0
    assert result["enabled"] is False


def test_temporary_change_never_writes_eeprom():
    fake = FakeMeter()
    run(meter.set_interval(fake.send, period_s=300, threshold_mpsas=17.5))
    assert not any(command[:1] in (b"P", b"T") for command in fake.sent)
    assert b"p0000000300x" in fake.sent and b"t00000017.50x" in fake.sent
    assert fake.eeprom == [5, 18.0]


def test_locked_meter_refuses_interval_change_before_sending():
    fake = FakeMeter(locked=True)
    with pytest.raises(meter.MeterLocked):
        run(meter.set_interval(fake.send, period_s=0, permanent=True))
    assert not any(command[:1] in (b"p", b"P") for command in fake.sent)


def test_lock_rule_ignoring_switch_allows_change_while_locked():
    fake = FakeMeter(locked=True, lock_rules="K,CrGT")
    result = run(meter.set_interval(fake.send, period_s=0))
    assert result["ram_period_s"] == 0


def test_change_that_does_not_stick_is_reported():
    fake = FakeMeter()

    async def ignoring_send(command, match, timeout):
        if command[:1] in (b"p", b"P"):
            command = b"Ix"  # meter replies with unchanged settings
        return await fake.send(command, match, timeout)

    with pytest.raises(meter.MeterRejected):
        run(meter.set_interval(ignoring_send, period_s=0))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"period_s": -1},
        {"period_s": 10**10},
        {"period_s": 60, "threshold_mpsas": -1},
        {"period_s": 60, "threshold_mpsas": 100},
    ],
)
def test_interval_values_are_validated(kwargs):
    with pytest.raises(ValueError):
        run(meter.set_interval(FakeMeter().send, **kwargs))
