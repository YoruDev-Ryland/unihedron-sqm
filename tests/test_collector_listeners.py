import asyncio

from sqm_service.collector import Collector


class Config:
    sqm_transport = "ethernet"
    sqm_host = None
    sqm_serial_device = None
    retention_days = 0


def test_listeners_are_called_and_failures_are_contained():
    collector = Collector(Config(), db=None, connection_lock=asyncio.Lock())
    seen = []

    def broken(_collector, _reading):
        raise RuntimeError("listener bug")

    collector.add_listener(broken)
    collector.add_listener(lambda c, reading: seen.append(reading))
    collector._notify({"mpsas": 21.0, "ts": 1.0})
    collector._notify(None)
    assert seen == [{"mpsas": 21.0, "ts": 1.0}, None]
