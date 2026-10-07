"""Ethernet client behavior against fake SQM-LE meters.

Real meters can be in interval-reporting mode, where they push a reading line
(with the serial number appended) every few seconds and may ignore commands.
The Lantronix module also buffers those lines while no client is connected and
delivers them all as soon as the next connection opens.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from sqm_service import sqm_client

INFO = b"i,00000004,00000003,00000080,00004171\r\n"


def report(mpsas: float) -> bytes:
    return (
        f"r, {mpsas:05.2f}m,0000000012Hz,0000000000c,0000000.000s, 012.4C,00004171\r\n"
    ).encode()


def answer(mpsas: float) -> bytes:
    return f"r, {mpsas:05.2f}m,0000000012Hz,0000000000c,0000000.000s, 012.4C\r\n".encode()


@contextlib.asynccontextmanager
async def fake_meter(*, stale=(), stream=None, interval=0.5, answers=True, extra=None):
    """Serve one fake meter on localhost.

    stale:   lines already buffered when a client connects.
    stream:  a line pushed every `interval` seconds (interval reporting).
    answers: whether `ix`/`rx` commands get their normal reply.
    extra:   further command -> reply bytes, answered like `ix`.
    """

    async def handle(reader, writer):
        connections.append(writer)
        for line in stale:
            writer.write(line)
        await writer.drain()

        async def push():
            while True:
                await asyncio.sleep(interval)
                writer.write(stream)
                await writer.drain()

        pusher = asyncio.create_task(push()) if stream else None
        try:
            while command := await reader.readuntil(b"x"):
                if not answers:
                    continue
                if extra and command.strip() in extra:
                    writer.write(extra[command.strip()])
                elif command.strip().endswith(b"ix"):
                    writer.write(INFO)
                elif command.strip().endswith(b"rx"):
                    writer.write(answer(21.0))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if pusher:
                pusher.cancel()
            writer.close()

    connections: list = []
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    fake_meter.connections = connections
    try:
        yield port
    finally:
        server.close()
        await server.wait_closed()


def run(coro):
    return asyncio.run(coro)


def test_plain_meter_still_answers_info_and_reading():
    async def scenario():
        async with fake_meter() as port:
            info = await sqm_client.get_info("127.0.0.1", port, 2, 5)
            reading = await sqm_client.get_reading("127.0.0.1", port, 2, 5)
        return info, reading

    info, reading = run(scenario())
    assert info["serial"] == 4171 and info["model"] == 3
    assert reading["mpsas"] == 21.0


def test_stale_buffered_reports_do_not_answer_info_query():
    async def scenario():
        async with fake_meter(stale=[report(1.0), report(1.0)]) as port:
            return await sqm_client.get_info("127.0.0.1", port, 2, 5)

    info = run(scenario())
    assert info["raw"] == INFO.decode().strip()
    assert info["model"] == 3


def test_stale_buffered_reports_are_not_saved_as_the_current_reading():
    async def scenario():
        async with fake_meter(stale=[report(1.0), report(1.0)]) as port:
            return await sqm_client.get_reading("127.0.0.1", port, 2, 5)

    assert run(scenario())["mpsas"] == 21.0


def test_streaming_meter_that_ignores_commands_is_identified_by_serial():
    async def scenario():
        async with fake_meter(
            stale=[report(1.0)], stream=report(18.5), interval=0.4, answers=False
        ) as port:
            return await sqm_client.get_info(
                "127.0.0.1", port, 2, 30, stream_grace=0.5
            )

    info = run(scenario())
    assert info["serial"] == 4171
    assert info["interval_reporting"] is True
    assert info["model"] is None


def test_streaming_meter_reading_uses_a_fresh_report():
    async def scenario():
        async with fake_meter(
            stale=[report(1.0)], stream=report(18.5), interval=0.4, answers=False
        ) as port:
            return await sqm_client.get_reading("127.0.0.1", port, 2, 5)

    assert run(scenario())["mpsas"] == 18.5


def test_silent_meter_times_out_with_clear_error():
    async def scenario():
        async with fake_meter(answers=False) as port:
            return await sqm_client.get_info("127.0.0.1", port, 2, 1)

    with pytest.raises(sqm_client.SQMError, match="no reply"):
        run(scenario())


def test_any_command_waits_for_its_own_reply_amid_pushed_reports():
    interval = b"0000000005s,0000000005s,00000018.00m,00000018.00m\r\n"

    async def scenario():
        async with fake_meter(
            stale=[report(1.0)], stream=report(18.5), interval=0.1,
            extra={b"Ix": interval},
        ) as port:
            return await sqm_client.query_tcp(
                "127.0.0.1", port, b"Ix", lambda line: line[:1].isdigit(), 2, 5
            )

    assert run(scenario()) == interval.decode().strip()


def test_session_sends_several_commands_over_one_connection():
    async def scenario():
        async with fake_meter(
            stale=[report(1.0)], stream=report(18.5), interval=0.1,
            extra={b"cx": b"c,00000019.40m,0000201.330s, 021.3C,00000008.71m, 023.8C\r\n"},
        ) as port:
            async with sqm_client.tcp_session("127.0.0.1", port, 2) as session:
                info = await session.identify(5)
                calibration = await session.send(
                    b"cx", lambda line: line.startswith("c,"), 5
                )
                reading = await session.send(
                    b"rx", lambda line: line.startswith("r,"), 5
                )
            return info, calibration, reading, len(fake_meter.connections)

    info, calibration, reading, connections = run(scenario())
    assert info["serial"] == 4171 and info["model"] == 3
    assert calibration.startswith("c,00000019.40m")
    assert reading.startswith("r, ")
    assert connections == 1


def test_refused_connection_is_retried_briefly():
    async def scenario():
        # Reserve a free port, start the meter on it only after a delay.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()

        async def handle(reader, writer):
            await reader.readuntil(b"x")
            writer.write(INFO)
            await writer.drain()
            writer.close()

        async def start_later():
            await asyncio.sleep(0.4)
            return await asyncio.start_server(handle, "127.0.0.1", port)

        starter = asyncio.create_task(start_later())
        try:
            return await sqm_client.get_info("127.0.0.1", port, 2, 5)
        finally:
            server = await starter
            server.close()
            await server.wait_closed()

    assert run(scenario())["serial"] == 4171
