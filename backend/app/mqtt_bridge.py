"""Optional MQTT bridge: republishes ingested telemetry over MQTT alongside UDP/TCP + SSE.

Disabled by default. A missing broker or missing `paho-mqtt` must never take the
ingestion service down, so every failure path here falls back to a no-op bridge.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)


class MqttBridge:
    def __init__(self, host: str, port: int = 1883, topic_prefix: str = "ecu", client_factory=None):
        self.host = host
        self.port = port
        self.topic_prefix = topic_prefix.rstrip("/")
        self._client_factory = client_factory or self._default_client
        self._client = self._client_factory()
        self._client.connect(self.host, self.port)
        self._client.loop_start()

    @staticmethod
    def _default_client():
        import paho.mqtt.client as mqtt
        return mqtt.Client()

    def topic_for(self, device_id: str) -> str:
        return f"{self.topic_prefix}/{device_id}/telemetry"

    def publish(self, device_id: str, data: dict) -> None:
        try:
            self._client.publish(self.topic_for(device_id), json.dumps(data), qos=0, retain=False)
        except Exception:
            logger.warning("mqtt publish failed", exc_info=True)

    def close(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass


def from_env() -> "MqttBridge | None":
    """Build a bridge from ECU_MQTT_* env vars, or None if disabled/unavailable."""
    if os.environ.get("ECU_MQTT_ENABLED", "").lower() not in ("1", "true", "yes"):
        return None
    host = os.environ.get("ECU_MQTT_HOST", "localhost")
    port = int(os.environ.get("ECU_MQTT_PORT", "1883"))
    prefix = os.environ.get("ECU_MQTT_TOPIC_PREFIX", "ecu")
    try:
        return MqttBridge(host, port, prefix)
    except Exception:
        logger.warning("ECU_MQTT_ENABLED set but broker unreachable; continuing without MQTT", exc_info=True)
        return None
