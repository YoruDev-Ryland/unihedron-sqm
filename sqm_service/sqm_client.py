"""Clients for the documented Unihedron SQM Ethernet and serial protocols."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import os
import time
from collections.abc import Callable, Iterable

import serial


class SQMError(Exception):
    """The meter is unreachable or returned an invalid response."""


# Bytes already waiting when a connection opens are stale: the Lantronix
# module buffers interval reports while no client is connected and delivers
# them all at once. They arrive immediately, so a short window catches them.
STALE_DRAIN_SECONDS = 0.25
# How long to keep waiting for an `ix` reply once a meter in interval-reporting
# mode has shown its serial number in a pushed report. Such meters may ignore
# commands entirely.
STREAM_GRACE_SECONDS = 3.0


def _is_interval_report(line: str) -> bool:
    """Interval reports are readings with the meter serial appended."""
    parts = [part.strip() for part in line.split(",")]
    return len(parts) >= 7 and parts[0].lower() == "r" and parts[6].isdigit()


async def _drain_stale(reader: asyncio.StreamReader) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + STALE_DRAIN_SECONDS
    while (remaining := deadline - loop.time()) > 0:
        try:
            if not await asyncio.wait_for(reader.read(4096), timeout=remaining):
                return
        except asyncio.TimeoutError:
            return


def _prefix(prefix: str) -> Callable[[str], bool]:
    return lambda line: line.lower().startswith(prefix)


# The XPort accepts one TCP client and needs a moment to release the previous
# connection; an immediate reconnect is refused. Retry refusals briefly.
CONNECT_RETRY_SECONDS = 1.5
CONNECT_RETRY_DELAY = 0.25


async def _open_tcp(
    host: str, port: int, connect_timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    retry_until = loop.time() + CONNECT_RETRY_SECONDS
    while True:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=connect_timeout
            )
        except ConnectionRefusedError as exc:
            if loop.time() >= retry_until:
                raise SQMError(f"connect to {host}:{port} failed: {exc}") from exc
            await asyncio.sleep(CONNECT_RETRY_DELAY)
        except (OSError, asyncio.TimeoutError) as exc:
            raise SQMError(f"connect to {host}:{port} failed: {exc}") from exc


class TcpSession:
    """One open connection to an Ethernet meter, for several commands."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def send(
        self,
        command: bytes,
        match: Callable[[str], bool],
        read_timeout: float,
        accept_interval_report: bool = False,
        stream_grace: float = STREAM_GRACE_SECONDS,
    ) -> str:
        """Send one command and return the first reply line accepted by `match`.

        Unrelated lines, such as pushed interval reports, are skipped. With
        `accept_interval_report`, a pushed report is returned instead when the
        expected reply has not arrived `stream_grace` seconds after it.
        """
        loop = asyncio.get_running_loop()
        self._writer.write(command)
        await self._writer.drain()
        deadline = loop.time() + read_timeout
        fallback: str | None = None
        last_line: str | None = None
        while (remaining := deadline - loop.time()) > 0:
            try:
                raw = await asyncio.wait_for(
                    self._reader.readuntil(b"\n"), timeout=remaining
                )
            except asyncio.IncompleteReadError as exc:
                raw = exc.partial
                deadline = loop.time()  # the meter closed the connection
            except asyncio.TimeoutError:
                break
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue
            last_line = line
            if match(line):
                return line
            if (
                accept_interval_report
                and fallback is None
                and _is_interval_report(line)
            ):
                fallback = line
                deadline = min(deadline, loop.time() + stream_grace)
        if fallback is not None:
            return fallback
        if last_line is None:
            raise SQMError(f"no reply within {read_timeout:g}s")
        raise SQMError(
            f"no reply to {command.strip().decode(errors='replace')!r} within "
            f"{read_timeout:g}s; meter sent {last_line!r}"
        )

    async def identify(
        self, read_timeout: float, stream_grace: float = STREAM_GRACE_SECONDS
    ) -> dict:
        line = await self.send(
            b"ix\r\n",
            _prefix("i,"),
            read_timeout,
            accept_interval_report=True,
            stream_grace=stream_grace,
        )
        if _is_interval_report(line):
            return info_from_interval_report(line)
        return parse_info(line)

    async def close(self) -> None:
        self._writer.close()
        try:
            await asyncio.wait_for(self._writer.wait_closed(), timeout=2)
        except (OSError, asyncio.TimeoutError):
            pass


@contextlib.asynccontextmanager
async def tcp_session(host: str, port: int, connect_timeout: float):
    reader, writer = await _open_tcp(host, port, connect_timeout)
    session = TcpSession(reader, writer)
    try:
        await _drain_stale(reader)
        yield session
    finally:
        await session.close()


async def query_tcp(
    host: str,
    port: int,
    command: bytes,
    match: Callable[[str], bool],
    connect_timeout: float,
    read_timeout: float,
    accept_interval_report: bool = False,
    stream_grace: float = STREAM_GRACE_SECONDS,
) -> str:
    """Open a connection, send one command, and return its reply line."""
    async with tcp_session(host, port, connect_timeout) as session:
        return await session.send(
            command, match, read_timeout, accept_interval_report, stream_grace
        )


def _open_serial(device: str, baud_rate: int) -> serial.Serial:
    try:
        return serial.Serial(
            port=device,
            baudrate=baud_rate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1,
            write_timeout=10,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
    except (OSError, ValueError, serial.SerialException) as exc:
        raise SQMError(f"open serial meter {device} failed: {exc}") from exc


def _ask_serial(
    connection: serial.Serial,
    command: bytes,
    match: Callable[[str], bool],
    read_timeout: float,
) -> str:
    try:
        # Discard anything buffered before this request, then skip
        # unrelated lines such as pushed interval reports.
        connection.reset_input_buffer()
        connection.write(command)
        connection.flush()
        deadline = time.monotonic() + read_timeout
        while (remaining := deadline - time.monotonic()) > 0:
            connection.timeout = remaining
            raw = connection.readline()
            if not raw:
                break
            line = raw.decode("ascii", errors="replace").strip()
            if line and match(line):
                return line
    except (OSError, ValueError, serial.SerialException) as exc:
        raise SQMError(f"serial meter {connection.port} failed: {exc}") from exc
    raise SQMError(
        f"serial meter {connection.port} did not reply within {read_timeout:g}s"
    )


class SerialSession:
    """One open serial port to a USB meter, for several commands."""

    def __init__(self, connection: serial.Serial) -> None:
        self._connection = connection

    async def send(
        self, command: bytes, match: Callable[[str], bool], read_timeout: float
    ) -> str:
        return await asyncio.to_thread(
            _ask_serial, self._connection, command, match, read_timeout
        )

    async def identify(self, read_timeout: float) -> dict:
        return parse_info(await self.send(b"ix\r", _prefix("i,"), read_timeout))


@contextlib.asynccontextmanager
async def serial_session(device: str, baud_rate: int):
    connection = await asyncio.to_thread(_open_serial, device, baud_rate)
    try:
        yield SerialSession(connection)
    finally:
        await asyncio.to_thread(connection.close)


async def query_serial(
    device: str,
    baud_rate: int,
    command: bytes,
    match: Callable[[str], bool],
    read_timeout: float,
) -> str:
    """Open the port, send one command, and return its reply line."""
    async with serial_session(device, baud_rate) as session:
        return await session.send(command, match, read_timeout)


def _strip_suffix(value: str, suffix: str) -> str:
    value = value.strip()
    if value.endswith(suffix):
        value = value[: -len(suffix)]
    return value.strip()


def parse_reading(line: str) -> dict:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 6 or parts[0].lower() != "r":
        raise SQMError(f"unexpected reading response: {line!r}")
    try:
        return {
            "mpsas": float(_strip_suffix(parts[1], "m")),
            "frequency_hz": int(_strip_suffix(parts[2], "Hz")),
            "period_counts": int(_strip_suffix(parts[3], "c")),
            "period_seconds": float(_strip_suffix(parts[4], "s")),
            "temperature_c": float(_strip_suffix(parts[5], "C")),
            "raw": line,
        }
    except ValueError as exc:
        raise SQMError(f"could not parse reading {line!r}: {exc}") from exc


def parse_info(line: str) -> dict:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 5 or parts[0].lower() != "i":
        raise SQMError(f"unexpected information response: {line!r}")
    try:
        return {
            "protocol": int(parts[1]),
            "model": int(parts[2]),
            "feature": int(parts[3]),
            "serial": int(parts[4]),
            "raw": line,
        }
    except ValueError as exc:
        raise SQMError(f"could not parse information {line!r}: {exc}") from exc


async def get_reading(
    host: str, port: int, connect_timeout: float, read_timeout: float
) -> dict:
    return parse_reading(
        await query_tcp(
            host, port, b"rx\r\n", _prefix("r,"), connect_timeout, read_timeout
        )
    )


def info_from_interval_report(line: str) -> dict:
    """Identify a meter that only pushes interval reports.

    Model, protocol, and feature are only available from an `ix` reply, so
    they stay unknown; the appended serial number still identifies the meter.
    """
    reading = parse_reading(line)
    return {
        "protocol": None,
        "model": None,
        "feature": None,
        "serial": int(line.split(",")[6]),
        "raw": reading["raw"],
        "interval_reporting": True,
    }


async def get_info(
    host: str,
    port: int,
    connect_timeout: float,
    read_timeout: float,
    stream_grace: float = STREAM_GRACE_SECONDS,
) -> dict:
    async with tcp_session(host, port, connect_timeout) as session:
        return await session.identify(read_timeout, stream_grace)


async def get_serial_reading(
    device: str, baud_rate: int, read_timeout: float
) -> dict:
    return parse_reading(
        await query_serial(device, baud_rate, b"rx\r", _prefix("r,"), read_timeout)
    )


async def get_serial_info(
    device: str, baud_rate: int, read_timeout: float
) -> dict:
    return parse_info(
        await query_serial(device, baud_rate, b"ix\r", _prefix("i,"), read_timeout)
    )


def serial_candidates(
    extra_paths: Iterable[str] = (), include_system: bool = True
) -> list[str]:
    """Return unique serial device paths, preferring stable by-id names."""

    paths: list[str] = [str(path) for path in extra_paths]
    if include_system:
        paths.extend(sorted(glob.glob("/dev/serial/by-id/*")))
        paths.extend(sorted(glob.glob("/dev/ttyUSB*")))
        paths.extend(sorted(glob.glob("/dev/ttyACM*")))
        if os.path.exists("/dev/sqm"):
            paths.insert(0, "/dev/sqm")

    found: list[str] = []
    targets: set[str] = set()
    for path in paths:
        if not os.path.exists(path):
            continue
        target = os.path.realpath(path)
        if target in targets:
            continue
        targets.add(target)
        found.append(path)
    return found


async def discover_serial_devices(
    paths: Iterable[str] | None = None,
    baud_rate: int = 115200,
    timeout: float = 2,
) -> list[dict]:
    candidates = serial_candidates(
        paths or (),
        include_system=paths is None,
    )
    semaphore = asyncio.Semaphore(4)

    async def probe(path: str) -> dict | None:
        try:
            async with semaphore:
                info = await get_serial_info(path, baud_rate, timeout)
            return {
                "transport": "serial",
                "path": path,
                "baud_rate": baud_rate,
                "source": "usb-serial",
                "verified": True,
                "device": info,
            }
        except SQMError:
            return None

    results = await asyncio.gather(*(probe(path) for path in candidates))
    return [result for result in results if result is not None]
