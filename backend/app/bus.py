"""In-process asyncio broadcast bus for SSE."""
from __future__ import annotations

import asyncio
import time


class EventBus:
    def __init__(self, maxsize: int = 1000):
        self.subs: set[asyncio.Queue] = set()
        self.maxsize = maxsize
        self._last_tel: dict[str, float] = {}
        self.published = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.maxsize)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.subs.discard(q)

    def publish(self, event: str, data: dict):
        self.published += 1
        for q in list(self.subs):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                # drop oldest to keep the stream live
                try:
                    q.get_nowait()
                    q.put_nowait((event, data))
                except Exception:
                    pass

    def publish_telemetry(self, device_id: str, data: dict, max_hz: float = 2.0) -> bool:
        """Downsample telemetry events to <= max_hz per device for the UI."""
        now = time.monotonic()
        last = self._last_tel.get(device_id, 0.0)
        if now - last < 1.0 / max_hz:
            return False
        self._last_tel[device_id] = now
        self.publish("telemetry", data)
        return True
