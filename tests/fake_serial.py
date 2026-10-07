"""Pseudo-terminal SQM-LU used by unit and container smoke tests."""

from __future__ import annotations

import os
import pty
import select
import threading


INFO_RESPONSE = b"i,00000004,00000003,00000080,00001234\r\n"
READING_RESPONSE = (
    b"r, 21.37m,0000000011Hz,0000000000c,0000000.000s, 011.8C\r\n"
)


class FakeSerialSQM:
    def __init__(self, responses: dict[bytes, bytes] | None = None) -> None:
        self.responses = responses or {
            b"ix": INFO_RESPONSE,
            b"rx": READING_RESPONSE,
        }
        self.commands: list[bytes] = []
        self._master, self._slave = pty.openpty()
        self.path = os.ttyname(self._slave)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> FakeSerialSQM:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        os.close(self._master)
        os.close(self._slave)

    def _serve(self) -> None:
        pending = b""
        while not self._stop.is_set():
            readable, _, _ = select.select([self._master], [], [], 0.05)
            if not readable:
                continue
            try:
                pending += os.read(self._master, 256)
            except OSError:
                return
            while b"\r" in pending:
                command, pending = pending.split(b"\r", 1)
                command = command.strip()
                if not command:
                    continue
                self.commands.append(command)
                response = self.responses.get(command)
                if response:
                    os.write(self._master, response)
