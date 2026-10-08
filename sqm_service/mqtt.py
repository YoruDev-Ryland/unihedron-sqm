"""Publish readings to MQTT, with Home Assistant discovery.

Publishing never blocks collection: paho runs its own network thread, and any
broker error only changes `status`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import sky

log = logging.getLogger("sqm.mqtt")

SENSORS = (
    ("mpsas", "Sky brightness", "mag/arcsec²", None, "mdi:weather-night"),
    ("temperature_c", "Sensor temperature", "°C", "temperature", None),
    ("nelm", "Naked-eye limiting magnitude", None, None, "mdi:eye"),
    ("bortle", "Bortle class", None, None, "mdi:star-four-points"),
)


def topics(base: str, serial: str) -> dict:
    return {"state": f"{base}/{serial}/state", "availability": f"{base}/{serial}/availability"}


def state_payload(reading: dict | None, collector_state: str) -> dict:
    payload: dict = {"collector": collector_state}
    if reading:
        payload.update(
            mpsas=reading["mpsas"],
            temperature_c=reading["temperature_c"],
            frequency_hz=reading["frequency_hz"],
            timestamp=datetime.fromtimestamp(reading["ts"], timezone.utc).isoformat(),
            **{k: v for k, v in sky.describe(reading["mpsas"]).items() if k != "bortle_name"},
        )
    return payload


def discovery_messages(prefix: str, base: str, serial: str, model: str, version: str) -> dict[str, dict]:
    names = topics(base, serial)
    device = {
        "identifiers": [f"sqm_{serial}"],
        "name": f"SQM {serial}",
        "manufacturer": "Unihedron",
        "model": f"SQM (model {model})",
        "sw_version": version,
    }
    common = {"state_topic": names["state"], "availability_topic": names["availability"], "device": device}
    messages = {}
    for key, name, unit, device_class, icon in SENSORS:
        config = {**common, "name": name, "unique_id": f"sqm_{serial}_{key}",
                  "value_template": f"{{{{ value_json.{key} }}}}", "state_class": "measurement"}
        if unit:
            config["unit_of_measurement"] = unit
        if device_class:
            config["device_class"] = device_class
        if icon:
            config["icon"] = icon
        messages[f"{prefix}/sensor/sqm_{serial}/{key}/config"] = config
    messages[f"{prefix}/binary_sensor/sqm_{serial}/collector/config"] = {
        **common, "name": "Collector", "unique_id": f"sqm_{serial}_collector",
        "device_class": "connectivity",
        "value_template": "{{ 'ON' if value_json.collector == 'collecting' else 'OFF' }}",
    }
    return messages


class MqttPublisher:
    def __init__(self, client_factory=None) -> None:
        self._factory = client_factory
        self._client = None
        self._names: dict | None = None
        self._discovery: dict[str, dict] = {}
        self.status = "off"

    def _new_client(self, serial: str):
        if self._factory is not None:
            return self._factory()
        import paho.mqtt.client as paho

        return paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=f"unihedron-sqm-{serial}")

    def configure(self, config: dict, serial: str, model: str, version: str) -> None:
        old_discovery = self._discovery
        new_discovery = (
            discovery_messages(config["discovery_prefix"], config["base_topic"], serial, model, version)
            if config["discovery"] else {}
        )
        if self._client is not None:
            for topic in set(old_discovery) - set(new_discovery):
                self._publish(topic, "", retain=True)
        self.stop()
        self._discovery = new_discovery
        if not config["host"]:
            self.status = "off"
            return
        self._names = topics(config["base_topic"], serial)
        client = self._new_client(serial)
        if config["username"]:
            client.username_pw_set(config["username"], config["password"] or None)
        if config["tls"]:
            client.tls_set()
        client.will_set(self._names["availability"], "offline", retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=120)
        client.on_connect = self._connected
        client.on_disconnect = self._disconnected
        self._client = client
        self.status = "connecting"
        try:
            client.connect_async(config["host"], config["port"])
            client.loop_start()
        except Exception as exc:
            self.status = f"error: {exc}"

    def _connected(self, client, _userdata, _flags, reason_code, _properties) -> None:
        # paho passes a ReasonCode; plain integers appear only in tests or old brokers.
        failed = reason_code.is_failure if hasattr(reason_code, "is_failure") else reason_code != 0
        if failed:
            self.status = f"error: {reason_code}"
            return
        self.status = "connected"
        self._publish(self._names["availability"], "online", retain=True)
        for topic, payload in self._discovery.items():
            self._publish(topic, json.dumps(payload), retain=True)

    def _disconnected(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        if self._client is not None:
            self.status = f"error: disconnected ({reason_code})"

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        try:
            self._client.publish(topic, payload, retain=retain)
        except Exception as exc:
            self.status = f"error: {exc}"
            log.warning("MQTT publish failed: %s", exc)

    def on_poll(self, collector, reading: dict | None) -> None:
        if self._client is None or self._names is None:
            return
        self._publish(self._names["state"], json.dumps(state_payload(reading, collector.state)))

    def stop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.disconnect()
                client.loop_stop()
            except Exception:
                pass
