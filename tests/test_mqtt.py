import json

from sqm_service import mqtt


class FakeClient:
    instances = []

    def __init__(self, *args, **kwargs):
        self.published = []
        self.will = None
        self.connected_to = None
        self.on_connect = self.on_disconnect = None
        FakeClient.instances.append(self)

    def username_pw_set(self, user, password): self.auth = (user, password)
    def tls_set(self): self.tls = True
    def will_set(self, topic, payload, retain): self.will = (topic, payload, retain)
    def reconnect_delay_set(self, min_delay, max_delay): self.delay = (min_delay, max_delay)
    def connect_async(self, host, port): self.connected_to = (host, port)
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def publish(self, topic, payload, retain=False, qos=0): self.published.append((topic, payload, retain))


CONFIG = {"host": "broker", "port": 1883, "username": "", "password": "", "tls": False,
          "base_topic": "sqm", "discovery_prefix": "homeassistant", "discovery": True}


def test_state_payload_includes_sky_values():
    payload = mqtt.state_payload({"mpsas": 21.09, "temperature_c": 8.4, "frequency_hz": 3, "ts": 0}, "collecting")
    assert payload["bortle"] == 4 and payload["nelm"] == 6.17
    assert payload["timestamp"] == "1970-01-01T00:00:00+00:00"
    assert payload["collector"] == "collecting"


def test_discovery_messages_describe_one_device():
    messages = mqtt.discovery_messages("homeassistant", "sqm", "4171", "3", "1.1.0")
    topics = set(messages)
    assert "homeassistant/sensor/sqm_4171/mpsas/config" in topics
    assert "homeassistant/binary_sensor/sqm_4171/collector/config" in topics
    mpsas = messages["homeassistant/sensor/sqm_4171/mpsas/config"]
    assert mpsas["state_topic"] == "sqm/4171/state"
    assert mpsas["availability_topic"] == "sqm/4171/availability"
    assert mpsas["device"]["identifiers"] == ["sqm_4171"]


def test_configure_connects_with_last_will_and_publishes_discovery():
    publisher = mqtt.MqttPublisher(client_factory=FakeClient)
    publisher.configure(CONFIG, "4171", "3", "1.1.0")
    client = FakeClient.instances[-1]
    assert client.connected_to == ("broker", 1883)
    assert client.will == ("sqm/4171/availability", "offline", True)
    client.on_connect(client, None, None, 0, None)
    assert ("sqm/4171/availability", "online", True) in client.published
    assert any(t.endswith("/mpsas/config") for t, _, _ in client.published)


def test_changing_discovery_prefix_clears_old_discovery():
    publisher = mqtt.MqttPublisher(client_factory=FakeClient)
    publisher.configure(CONFIG, "4171", "3", "1.1.0")
    first = FakeClient.instances[-1]
    first.on_connect(first, None, None, 0, None)
    publisher.configure({**CONFIG, "discovery_prefix": "ha"}, "4171", "3", "1.1.0")
    assert ("homeassistant/sensor/sqm_4171/mpsas/config", "", True) in first.published


def test_changing_base_topic_updates_discovery_in_place():
    publisher = mqtt.MqttPublisher(client_factory=FakeClient)
    publisher.configure({**CONFIG, "base_topic": "observatory"}, "4171", "3", "1.1.0")
    client = FakeClient.instances[-1]
    client.on_connect(client, None, None, 0, None)
    configs = {t: p for t, p, _ in client.published if t.endswith("/mpsas/config")}
    assert json.loads(configs["homeassistant/sensor/sqm_4171/mpsas/config"])["state_topic"] == "observatory/4171/state"


def test_blank_host_turns_publishing_off():
    publisher = mqtt.MqttPublisher(client_factory=FakeClient)
    publisher.configure({**CONFIG, "host": ""}, "4171", "3", "1.1.0")
    assert publisher.status == "off"
    publisher.on_poll(type("C", (), {"state": "collecting"})(), {"mpsas": 21.0, "temperature_c": 1, "frequency_hz": 1, "ts": 0})


def test_poll_publishes_state_and_broker_errors_never_raise():
    publisher = mqtt.MqttPublisher(client_factory=FakeClient)
    publisher.configure(CONFIG, "4171", "3", "1.1.0")
    client = FakeClient.instances[-1]
    collector = type("C", (), {"state": "collecting"})()
    publisher.on_poll(collector, {"mpsas": 21.0, "temperature_c": 1, "frequency_hz": 1, "ts": 0})
    topic, payload, retain = client.published[-1]
    assert topic == "sqm/4171/state" and json.loads(payload)["mpsas"] == 21.0
    def explode(*a, **k): raise OSError("broker gone")
    client.publish = explode
    publisher.on_poll(collector, None)  # must not raise
    assert publisher.status.startswith("error")


def test_refused_connection_reports_error_and_publishes_nothing():
    from paho.mqtt.reasoncodes import ReasonCode
    from paho.mqtt.packettypes import PacketTypes

    publisher = mqtt.MqttPublisher(client_factory=FakeClient)
    publisher.configure(CONFIG, "4171", "3", "1.1.0")
    client = FakeClient.instances[-1]
    refused = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")
    client.on_connect(client, None, None, refused, None)
    assert publisher.status.startswith("error")
    assert client.published == []
    accepted = ReasonCode(PacketTypes.CONNACK, "Success")
    client.on_connect(client, None, None, accepted, None)
    assert publisher.status == "connected"


class SlowClient(FakeClient):
    """A client whose network thread is stuck, e.g. in a connect timeout."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.events.append(("publish", topic, payload, retain))
        super().publish(topic, payload, retain, qos)

    def disconnect(self):
        self.events.append(("disconnect",))

    def loop_stop(self):
        import time
        time.sleep(1.0)
        self.events.append(("loop_stop",))


def test_stop_marks_offline_then_disconnects_without_blocking():
    import time

    publisher = mqtt.MqttPublisher(client_factory=SlowClient)
    publisher.configure(CONFIG, "4171", "3", "1.1.0")
    client = FakeClient.instances[-1]
    client.on_connect(client, None, None, 0, None)
    started = time.monotonic()
    worker = publisher.stop()
    assert time.monotonic() - started < 0.2
    worker.join(timeout=3)
    assert client.events[-3:] == [
        ("publish", "sqm/4171/availability", "offline", True),
        ("disconnect",),
        ("loop_stop",),
    ]


def test_reconfigure_never_blocks_on_a_stuck_client():
    import time

    publisher = mqtt.MqttPublisher(client_factory=SlowClient)
    publisher.configure(CONFIG, "4171", "3", "1.1.0")
    started = time.monotonic()
    publisher.configure({**CONFIG, "host": "other"}, "4171", "3", "1.1.0")
    assert time.monotonic() - started < 0.2
