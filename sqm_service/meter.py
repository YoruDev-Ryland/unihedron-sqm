"""Read meter details and change interval reporting on an SQM.

Command formats follow the Unihedron SQM-LE Operator's Manual (revision
20260924), chapter 8. Everything here talks to the meter through a `send`
coroutine, `send(command, match, timeout) -> line`, which returns the first
reply line accepted by `match`. The collector provides one that shares its
connection lock, so these commands never collide with a poll.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from .sqm_client import SQMError, parse_info

Send = Callable[[bytes, Callable[[str], bool], float], Awaitable[str]]
Identify = Callable[[], Awaitable[dict]]

COMMAND_TIMEOUT = 5.0
# An SQM-LU-DL answers `L1x` at once; other meters stay silent, so a short wait
# keeps the Meter page quick for the many USB meters without a logger.
LOGGER_PROBE_TIMEOUT = 1.5
MAX_PERIOD_S = 7 * 24 * 3600
MAX_THRESHOLD_MPSAS = 30.0

# Minimum firmware feature numbers, from the manual's command tables.
FEATURE_INTERVAL = 13
FEATURE_LOCK_RULES = 46
FEATURE_FRESHNESS = 58

# The manual shows "I," before the values; feature-80 firmware omits it.
INTERVAL_PATTERN = re.compile(
    r"^(?:I,)?(\d{10})s,(\d{10})s,(-?\d{8}\.\d{2})m,(-?\d{8}\.\d{2})m$"
)
LOCK_SWITCH_PATTERN = re.compile(r"^z\w*([LU])$")
LOCK_RULES_PATTERN = re.compile(r"^K,([Cc])([Rr])([Gg])([Tt])$")


class MeterLocked(SQMError):
    """The lock switch blocks this change."""


class MeterRejected(SQMError):
    """The meter replied, but the requested change did not take effect."""


# ------------------------------------------------------------------ parsers


def _number(field: str, suffix: str) -> float:
    field = field.strip()
    if not field.endswith(suffix):
        raise ValueError(f"{field!r} lacks unit {suffix!r}")
    return float(field[: -len(suffix)])


def is_interval_reply(line: str) -> bool:
    return bool(INTERVAL_PATTERN.match(line.strip()))


def parse_interval(line: str) -> dict:
    found = INTERVAL_PATTERN.match(line.strip())
    if not found:
        raise SQMError(f"unexpected interval settings response: {line!r}")
    eeprom_period, ram_period, eeprom_threshold, ram_threshold = found.groups()
    return {
        "eeprom_period_s": int(eeprom_period),
        "ram_period_s": int(ram_period),
        "eeprom_threshold_mpsas": float(eeprom_threshold),
        "ram_threshold_mpsas": float(ram_threshold),
    }


def is_calibration_reply(line: str) -> bool:
    return line.startswith("c,")


def parse_calibration(line: str) -> dict:
    parts = line.split(",")
    if len(parts) < 6 or parts[0] != "c":
        raise SQMError(f"unexpected calibration response: {line!r}")
    try:
        return {
            "light_offset_mpsas": _number(parts[1], "m"),
            "dark_period_s": _number(parts[2], "s"),
            "light_temperature_c": _number(parts[3], "C"),
            "sensor_offset_mpsas": _number(parts[4], "m"),
            "dark_temperature_c": _number(parts[5], "C"),
        }
    except ValueError as exc:
        raise SQMError(f"could not parse calibration {line!r}: {exc}") from exc


def is_lock_switch_reply(line: str) -> bool:
    return bool(LOCK_SWITCH_PATTERN.match(line.strip()))


def parse_lock_switch(line: str) -> bool:
    """True when the physical lock switch is in the locked position."""
    found = LOCK_SWITCH_PATTERN.match(line.strip())
    if not found:
        raise SQMError(f"unexpected lock status response: {line!r}")
    return found.group(1) == "L"


def is_lock_rules_reply(line: str) -> bool:
    return bool(LOCK_RULES_PATTERN.match(line.strip()))


def parse_lock_rules(line: str) -> dict:
    """Which kinds of change respect the lock switch (upper case = respect)."""
    found = LOCK_RULES_PATTERN.match(line.strip())
    if not found:
        raise SQMError(f"unexpected lock settings response: {line!r}")
    calibration, interval, configuration, settings = found.groups()
    return {
        "calibration": calibration.isupper(),
        "report_interval": interval.isupper(),
        "configuration": configuration.isupper(),
        "lock_settings": settings.isupper(),
    }


FRESHNESS = {"F": "frequency", "P": "period", "S": "stale"}


def is_freshness_reply(line: str) -> bool:
    parts = [part.strip() for part in line.split(",")]
    return len(parts) >= 8 and parts[0] == "r" and parts[7] in FRESHNESS


def parse_freshness(line: str) -> str:
    if not is_freshness_reply(line):
        raise SQMError(f"unexpected r1x response: {line!r}")
    return FRESHNESS[line.split(",")[7].strip()]


# ------------------------------------------------------------------ details


def _interval_view(settings: dict) -> dict:
    return {
        **settings,
        "enabled": settings["ram_period_s"] > 0,
        "enabled_at_boot": settings["eeprom_period_s"] > 0,
    }


async def _default_identify(send: Send) -> dict:
    return parse_info(
        await send(b"ix", lambda line: line.startswith("i,"), COMMAND_TIMEOUT)
    )


async def read_details(
    send: Send, identify: Identify | None = None, probe_logger: bool = False
) -> dict:
    """Collect identity, calibration, interval, lock, and freshness.

    `identify` returns unit information, falling back to a pushed interval
    report when the meter does not answer `ix`; that fallback is how a meter
    whose commands are not getting through is recognised.
    """
    details: dict = {
        "health": {"status": "ok", "message": "The meter answers commands."},
        "info": None,
        "calibration": None,
        "interval": None,
        "lock": None,
        "freshness": None,
        "logger": None,
        "errors": {},
    }
    try:
        info = await (identify() if identify else _default_identify(send))
    except SQMError as exc:
        details["health"] = {"status": "unreachable", "message": str(exc)}
        return details
    details["info"] = info

    if info.get("interval_reporting"):
        details["health"] = {
            "status": "commands_unanswered",
            "message": (
                "The meter sends interval reports but does not answer "
                "commands. On an SQM-LE, check the XPort web page: with "
                "Channel 1 flow control set to CTS/RTS, a configurable pin "
                "must be set to \"HW Flow Control In\"; otherwise set flow "
                "control to None."
            ),
        }
        return details

    feature = info.get("feature") or 0

    async def ask(key, command, match, parse, minimum_feature=0):
        if feature < minimum_feature:
            return None
        try:
            return parse(await send(command, match, COMMAND_TIMEOUT))
        except SQMError as exc:
            details["errors"][key] = str(exc)
            return None

    details["calibration"] = await ask(
        "calibration", b"cx", is_calibration_reply, parse_calibration
    )
    interval = await ask(
        "interval", b"Ix", is_interval_reply, parse_interval, FEATURE_INTERVAL
    )
    details["interval"] = _interval_view(interval) if interval else None
    locked = await ask(
        "lock", b"zcalDx", is_lock_switch_reply, parse_lock_switch
    )
    rules = await ask(
        "lock_rules", b"Kx", is_lock_rules_reply, parse_lock_rules,
        FEATURE_LOCK_RULES,
    )
    if locked is not None:
        details["lock"] = {"locked": locked, "rules": rules}
    details["freshness"] = await ask(
        "freshness", b"r1x", is_freshness_reply, parse_freshness,
        FEATURE_FRESHNESS,
    )
    if probe_logger:
        # Only an SQM-LU-DL answers `L1x`; silence just means no logger, so it
        # is not reported as an error.
        try:
            line = await send(b"L1x", lambda reply: reply.startswith("L1,"), LOGGER_PROBE_TIMEOUT)
            details["logger"] = {"records": int(line.split(",", 1)[1])}
        except (SQMError, ValueError, IndexError):
            details["logger"] = None
    return details


# --------------------------------------------------------------- interval


async def _interval_command(send: Send, command: bytes) -> dict:
    return parse_interval(await send(command, is_interval_reply, COMMAND_TIMEOUT))


async def set_interval(
    send: Send,
    period_s: int,
    threshold_mpsas: float | None = None,
    permanent: bool = False,
) -> dict:
    """Change interval reporting; a period of 0 turns it off.

    RAM is always written first. EEPROM, which has limited write endurance,
    is written only when `permanent` is set. The settings are read back
    afterwards and any value that did not take effect raises MeterRejected.
    """
    if not 0 <= period_s <= MAX_PERIOD_S:
        raise ValueError(f"period must be 0 to {MAX_PERIOD_S} seconds")
    if threshold_mpsas is not None and not (
        0 <= threshold_mpsas <= MAX_THRESHOLD_MPSAS
    ):
        raise ValueError(
            f"threshold must be 0 to {MAX_THRESHOLD_MPSAS:g} mag/arcsec²"
        )

    locked = parse_lock_switch(
        await send(b"zcalDx", is_lock_switch_reply, COMMAND_TIMEOUT)
    )
    if locked:
        try:
            rules = parse_lock_rules(
                await send(b"Kx", is_lock_rules_reply, COMMAND_TIMEOUT)
            )
            respects_lock = rules["report_interval"]
        except SQMError:
            respects_lock = True  # older firmware always respects the lock
        if respects_lock:
            raise MeterLocked(
                "The meter's lock switch is locked and blocks interval "
                "changes. Unlock it to change these settings."
            )

    await _interval_command(send, b"p%010dx" % period_s)
    if permanent:
        await _interval_command(send, b"P%010dx" % period_s)
    if threshold_mpsas is not None:
        value = f"{threshold_mpsas:011.2f}".encode()
        await _interval_command(send, b"t" + value + b"x")
        if permanent:
            await _interval_command(send, b"T" + value + b"x")

    settings = await _interval_command(send, b"Ix")
    wrong = settings["ram_period_s"] != period_s or (
        permanent and settings["eeprom_period_s"] != period_s
    )
    if threshold_mpsas is not None:
        wrong = wrong or abs(settings["ram_threshold_mpsas"] - threshold_mpsas) > 0.005
        if permanent:
            wrong = wrong or (
                abs(settings["eeprom_threshold_mpsas"] - threshold_mpsas) > 0.005
            )
    if wrong:
        raise MeterRejected(
            "The meter did not apply the change; its interval settings are "
            "unchanged. Check the lock switch and firmware version."
        )
    return _interval_view(settings)
