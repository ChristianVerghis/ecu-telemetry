import json

from app import mqtt_bridge
from app.bus import EventBus
from app.mqtt_bridge import MqttBridge


class FakeMqttClient:
    def __init__(self):
        self.connected = None
        self.loop_started = False
        self.published = []
        self.disconnected = False

    def connect(self, host, port):
        self.connected = (host, port)

    def loop_start(self):
        self.loop_started = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload))

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnected = True


def make_bridge(**kwargs):
    client = FakeMqttClient()
    bridge = MqttBridge(host="broker.local", client_factory=lambda: client, **kwargs)
    return bridge, client


def test_topic_for_uses_prefix():
    bridge, _ = make_bridge(topic_prefix="ecu/")
    assert bridge.topic_for("ECU-0001") == "ecu/ECU-0001/telemetry"


def test_publish_sends_json_payload_to_device_topic():
    bridge, client = make_bridge()
    bridge.publish("ECU-0002", {"rpm": 4200, "temp_c": 61.5})
    assert len(client.published) == 1
    topic, payload = client.published[0]
    assert topic == "ecu/ECU-0002/telemetry"
    assert json.loads(payload) == {"rpm": 4200, "temp_c": 61.5}


def test_publish_swallows_client_errors():
    bridge, client = make_bridge()

    def boom(*a, **k):
        raise RuntimeError("broker gone")

    client.publish = boom
    bridge.publish("ECU-0003", {"rpm": 1})  # must not raise


def test_connect_uses_given_host_and_port():
    bridge, client = make_bridge(port=18830)
    assert client.connected == ("broker.local", 18830)
    assert client.loop_started


def test_close_stops_and_disconnects():
    bridge, client = make_bridge()
    bridge.close()
    assert client.disconnected
    assert not client.loop_started


def test_from_env_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ECU_MQTT_ENABLED", raising=False)
    assert mqtt_bridge.from_env() is None


def test_from_env_builds_bridge_when_enabled(monkeypatch):
    monkeypatch.setenv("ECU_MQTT_ENABLED", "true")
    monkeypatch.setenv("ECU_MQTT_HOST", "mqtt.example")
    monkeypatch.setenv("ECU_MQTT_PORT", "1884")
    monkeypatch.setenv("ECU_MQTT_TOPIC_PREFIX", "fleet")

    client = FakeMqttClient()
    monkeypatch.setattr(mqtt_bridge.MqttBridge, "_default_client", staticmethod(lambda: client))

    bridge = mqtt_bridge.from_env()
    assert bridge is not None
    assert bridge.host == "mqtt.example"
    assert bridge.port == 1884
    assert bridge.topic_prefix == "fleet"
    assert client.connected == ("mqtt.example", 1884)


def test_from_env_returns_none_when_broker_unreachable(monkeypatch):
    monkeypatch.setenv("ECU_MQTT_ENABLED", "1")

    def failing_client():
        raise OSError("connection refused")

    monkeypatch.setattr(mqtt_bridge.MqttBridge, "_default_client", staticmethod(failing_client))
    assert mqtt_bridge.from_env() is None


def test_event_bus_forwards_downsampled_telemetry_to_bridge():
    class RecordingBridge:
        def __init__(self):
            self.calls = []

        def publish(self, device_id, data):
            self.calls.append((device_id, data))

    bridge = RecordingBridge()
    bus = EventBus(mqtt_bridge=bridge)
    assert bus.publish_telemetry("ECU-0001", {"rpm": 1}, max_hz=2.0) is True
    assert bus.publish_telemetry("ECU-0001", {"rpm": 2}, max_hz=2.0) is False  # downsampled
    assert bridge.calls == [("ECU-0001", {"rpm": 1})]


def test_event_bus_without_bridge_does_not_error():
    bus = EventBus()
    assert bus.publish_telemetry("ECU-0001", {"rpm": 1}) is True
