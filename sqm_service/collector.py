"""Self-healing background collector for one Ethernet or serial SQM."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import sqm_client
from .config import Config
from .db import Database

log = logging.getLogger("sqm.collector")


class Collector:
    def __init__(
        self, config: Config, db: Database, connection_lock: asyncio.Lock
    ) -> None:
        self.config = config
        self.db = db
        self.connection_lock = connection_lock
        self.last_success_ts: float | None = None
        self.last_error: str | None = None
        self.state = "starting"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run(), name="sqm-collector")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def configured(self) -> bool:
        if self.config.sqm_transport == "serial":
            return bool(self.config.sqm_serial_device)
        return bool(self.config.sqm_host)

    @property
    def endpoint_key(self) -> str | None:
        if self.config.sqm_transport == "serial":
            if not self.config.sqm_serial_device:
                return None
            return (
                f"serial:{self.config.sqm_serial_device}:"
                f"{self.config.sqm_baud_rate}"
            )
        if not self.config.sqm_host:
            return None
        return f"ethernet:{self.config.sqm_host}:{self.config.sqm_port}"

    @property
    def endpoint_label(self) -> str | None:
        if self.config.sqm_transport == "serial":
            return self.config.sqm_serial_device
        if not self.config.sqm_host:
            return None
        return f"{self.config.sqm_host}:{self.config.sqm_port}"

    def configure_ethernet(self, host: str, port: int) -> None:
        self.config.sqm_transport = "ethernet"
        self.config.sqm_host = host
        self.config.sqm_port = port
        self._configuration_changed()

    def configure_serial(self, device: str, baud_rate: int) -> None:
        self.config.sqm_transport = "serial"
        self.config.sqm_serial_device = device
        self.config.sqm_baud_rate = baud_rate
        self._configuration_changed()

    def _configuration_changed(self) -> None:
        self.last_success_ts = None
        self.last_error = None
        self._wake.set()

    async def _get_info(self) -> dict:
        if self.config.sqm_transport == "serial":
            device = self.config.sqm_serial_device
            if not device:
                raise sqm_client.SQMError("no serial meter is configured")
            return await sqm_client.get_serial_info(
                device,
                self.config.sqm_baud_rate,
                self.config.read_timeout,
            )
        host = self.config.sqm_host
        if not host:
            raise sqm_client.SQMError("no Ethernet meter is configured")
        return await sqm_client.get_info(
            host,
            self.config.sqm_port,
            self.config.connect_timeout,
            self.config.read_timeout,
        )

    async def _get_reading(self) -> dict:
        if self.config.sqm_transport == "serial":
            device = self.config.sqm_serial_device
            if not device:
                raise sqm_client.SQMError("no serial meter is configured")
            return await sqm_client.get_serial_reading(
                device,
                self.config.sqm_baud_rate,
                self.config.read_timeout,
            )
        host = self.config.sqm_host
        if not host:
            raise sqm_client.SQMError("no Ethernet meter is configured")
        return await sqm_client.get_reading(
            host,
            self.config.sqm_port,
            self.config.connect_timeout,
            self.config.read_timeout,
        )

    @contextlib.asynccontextmanager
    async def session(self):
        """Hold the meter connection for several commands.

        Polling waits on the same lock, and one connection avoids the XPort
        refusing a reconnect made right after the previous one closed.
        """
        async with self.connection_lock:
            if self.config.sqm_transport == "serial":
                device = self.config.sqm_serial_device
                if not device:
                    raise sqm_client.SQMError("no serial meter is configured")
                async with sqm_client.serial_session(
                    device, self.config.sqm_baud_rate
                ) as session:
                    yield session
                return
            host = self.config.sqm_host
            if not host:
                raise sqm_client.SQMError("no Ethernet meter is configured")
            async with sqm_client.tcp_session(
                host, self.config.sqm_port, self.config.connect_timeout
            ) as session:
                yield session

    async def _device_info(self, endpoint_key: str) -> None:
        if (
            self.db.device_info() is not None
            and self.db.get_setting("device_info_endpoint") == endpoint_key
        ):
            return
        try:
            async with self.connection_lock:
                info = await self._get_info()
            await asyncio.to_thread(
                self.db.upsert_device_info, info, endpoint_key
            )
            log.info(
                "connected to SQM model=%s serial=%s over %s at %s",
                info["model"],
                info["serial"],
                self.config.sqm_transport,
                self.endpoint_label,
            )
            await asyncio.sleep(1)
        except sqm_client.SQMError as exc:
            log.warning("could not read meter information: %s", exc)

    async def _read_once(self, tries: int = 3) -> dict:
        last_error: sqm_client.SQMError | None = None
        for attempt in range(tries):
            try:
                async with self.connection_lock:
                    return await self._get_reading()
            except sqm_client.SQMError as exc:
                last_error = exc
                if attempt < tries - 1:
                    await asyncio.sleep(2)
        assert last_error is not None
        raise last_error

    async def _wait(self) -> None:
        # A configuration change may arrive between a poll and this wait.
        # Consume an already-set wake event instead of accidentally erasing it.
        if self._wake.is_set():
            self._wake.clear()
            return
        try:
            await asyncio.wait_for(
                self._wake.wait(), timeout=self.config.poll_interval
            )
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            endpoint_key = self.endpoint_key
            if not endpoint_key:
                self.state = "unconfigured"
                self.last_error = None
                await self._wait()
                continue

            self.state = "connecting"
            try:
                await self._device_info(endpoint_key)
                reading = await self._read_once()
                await loop.run_in_executor(None, self.db.insert_reading, reading)
                self.last_success_ts = time.time()
                self.last_error = None
                self.state = "collecting"
                if self.config.retention_days > 0:
                    removed = await loop.run_in_executor(
                        None, self.db.prune, self.config.retention_days
                    )
                    if removed:
                        log.info("pruned %d expired readings", removed)
            except sqm_client.SQMError as exc:
                self.last_error = str(exc)
                self.state = "offline"
                log.warning("poll failed: %s", exc)
            except Exception:
                self.last_error = "unexpected collector error; see container logs"
                self.state = "error"
                log.exception("unexpected collector error")
            await self._wait()
