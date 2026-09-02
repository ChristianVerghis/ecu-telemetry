"""UDP telemetry ingest + TCP control channel + device stats / offline detection."""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field

from . import protocol as P
from .db import Database, iso

OFFLINE_AFTER_S = 15.0
LOSS_WINDOW = 200  # telemetry frames considered for rolling loss %


@dataclass
class DeviceStats:
    frames_rx: int = 0
    bad_crc: int = 0
    seq_gaps: int = 0
    reconnects: int = 0
    last_seen: float | None = None
    last_seq: int | None = None
    tcp_connected: bool = False
    online: bool = False
    window: deque = field(default_factory=lambda: deque(maxlen=LOSS_WINDOW))  # (received=1, expected=1+gap)
    tx_seq: int = 0

    @property
    def loss_pct(self) -> float:
        if not self.window:
            return 0.0
        exp = sum(e for _, e in self.window)
        rx = sum(r for r, _ in self.window)
        return round(max(0.0, 100.0 * (1 - rx / exp)), 2) if exp else 0.0

    def note_seq(self, seq: int):
        if self.last_seq is not None:
            delta = (seq - self.last_seq) & 0xFFFFFFFF
            if delta == 0 or delta > 0x7FFFFFFF:
                return  # duplicate / reordered old frame
            gap = delta - 1
            if gap > 10_000:  # device reset its sequence: treat as a restart, not loss
                gap = 0
            self.seq_gaps += gap
            self.window.append((1, 1 + gap))
        else:
            self.window.append((1, 1))
        self.last_seq = seq

    def as_dict(self) -> dict:
        return {"frames_rx": self.frames_rx, "bad_crc": self.bad_crc, "seq_gaps": self.seq_gaps,
                "loss_pct": self.loss_pct, "reconnects": self.reconnects, "tcp_connected": self.tcp_connected,
                "online": self.online, "last_seen": self.last_seen, "last_seen_iso": iso(self.last_seen),
                "last_seq": self.last_seq}


class Ingest:
    def __init__(self, db: Database, bus, engine, udp_port: int = 8781, tcp_port: int = 8782, host: str = "0.0.0.0"):
        self.db, self.bus, self.engine = db, bus, engine
        self.host, self.udp_port, self.tcp_port = host, udp_port, tcp_port
        self.stats: dict[str, DeviceStats] = {}
        self.writers: dict[str, asyncio.StreamWriter] = {}
        self.latest: dict[str, dict] = {}
        self.global_bad_frames = 0
        self.total_frames = 0
        self._udp_transport = None
        self._tcp_server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []
        self.started_at = time.time()
        for dev, s in db.load_stats().items():
            st = self.st(dev)
            st.frames_rx, st.bad_crc, st.seq_gaps, st.reconnects = s["frames_rx"], s["bad_crc"], s["seq_gaps"], s["reconnects"]
            st.last_seen = s["last_seen"]

    def st(self, device_id: str) -> DeviceStats:
        s = self.stats.get(device_id)
        if s is None:
            s = self.stats[device_id] = DeviceStats()
        return s

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        loop = asyncio.get_running_loop()
        self._udp_transport, _ = await loop.create_datagram_endpoint(
            lambda: _UdpProto(self), local_addr=(self.host, self.udp_port))
        self.udp_port = self._udp_transport.get_extra_info("sockname")[1]
        self._tcp_server = await asyncio.start_server(self._handle_tcp, self.host, self.tcp_port)
        self.tcp_port = self._tcp_server.sockets[0].getsockname()[1]
        self._tasks = [asyncio.create_task(self._offline_loop()), asyncio.create_task(self._stats_loop())]

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if self._udp_transport:
            self._udp_transport.close()
        if self._tcp_server:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()
        for w in list(self.writers.values()):
            w.close()
        self._persist_stats()

    def _persist_stats(self):
        for dev, s in self.stats.items():
            self.db.save_stats(dev, s.as_dict())

    async def _stats_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                await asyncio.to_thread(self._persist_stats)
                self.engine.tick()
            except Exception as e:  # pragma: no cover
                print("stats loop error:", e)

    async def _offline_loop(self):
        while True:
            await asyncio.sleep(1)
            now = time.time()
            for dev, s in self.stats.items():
                if s.online and s.last_seen and now - s.last_seen > OFFLINE_AFTER_S:
                    self._set_online(dev, False)
            # also handle devices from DB never seen in this process
            for d in self.db.list_devices():
                if d["online"] and d["device_id"] not in self.stats and d["last_seen"] and now - d["last_seen"] > OFFLINE_AFTER_S:
                    self._set_online(d["device_id"], False)

    def _set_online(self, device_id: str, online: bool):
        s = self.st(device_id)
        s.online = online
        self.db.set_online(device_id, online)
        if online:
            self.engine.on_online(device_id)
        else:
            self.engine.on_offline(device_id)
        self.bus.publish("device", {"device_id": device_id, "event": "online" if online else "offline",
                                    "online": online, "ts": iso(time.time())})

    def _seen(self, device_id: str):
        s = self.st(device_id)
        s.last_seen = time.time()
        if not s.online:
            self._set_online(device_id, True)

    # ------------------------------------------------------------ UDP
    def on_datagram(self, data: bytes):
        self.total_frames += 1
        try:
            hdr = P.Header.unpack(data)
        except P.ProtocolError:
            self.global_bad_frames += 1
            return
        s = self.st(hdr.device_id)
        try:
            frame = P.decode(data)
        except P.ProtocolError:
            s.bad_crc += 1  # attributable to a device (header parsed); global_bad_frames counts unattributable ones
            return
        if frame.msg_type != P.MsgType.TELEMETRY or not isinstance(frame.payload, P.Telemetry):
            return
        self.handle_telemetry(frame)

    def handle_telemetry(self, frame: P.Frame):
        dev = frame.device_id
        s = self.st(dev)
        s.frames_rx += 1
        s.note_seq(frame.header.seq)
        now = time.time()
        t: P.Telemetry = frame.payload
        d = t.as_dict()
        d.update({"device_id": dev, "ts": now, "ts_iso": iso(now), "device_ts_ms": frame.header.ts_ms,
                  "seq": frame.header.seq, "fw_version": P.fw_decode(frame.header.fw_version), "loss_pct": s.loss_pct})
        self.latest[dev] = d
        self.db.buffer_telemetry(dev, now, frame.header.seq, d)
        self.db.upsert_device(dev, state=t.state, lat=t.lat, lon=t.lon, fw_version=P.fw_decode(frame.header.fw_version),
                              fw_version_u32=frame.header.fw_version, last_seen=now)
        self._seen(dev)
        d["fired"] = self.engine.on_telemetry(dev, d, now, s.loss_pct)
        self.bus.publish_telemetry(dev, d)

    # ------------------------------------------------------------ TCP
    async def _handle_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        dec = P.StreamDecoder()
        device_id: str | None = None
        peer = writer.get_extra_info("peername")
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for frame in dec.feed(data):
                    if device_id is None:
                        device_id = frame.device_id
                        await self._register(device_id, writer)
                    elif frame.device_id != device_id:
                        continue
                    self.total_frames += 1
                    await self._dispatch(frame, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as e:  # pragma: no cover
            print(f"tcp handler error {peer}: {e!r}")
        finally:
            if device_id and self.writers.get(device_id) is writer:
                del self.writers[device_id]
                self.st(device_id).tcp_connected = False
                self.bus.publish("device", {"device_id": device_id, "event": "tcp_disconnected", "ts": iso(time.time())})
            with contextlib.suppress(Exception):
                writer.close()

    async def _register(self, device_id: str, writer: asyncio.StreamWriter):
        s = self.st(device_id)
        old = self.writers.get(device_id)
        if old is not None and old is not writer:
            with contextlib.suppress(Exception):
                old.close()
        if s.frames_rx or old is not None or s.last_seen:
            s.reconnects += 1
        self.writers[device_id] = writer
        s.tcp_connected = True
        self.bus.publish("device", {"device_id": device_id, "event": "tcp_connected", "ts": iso(time.time())})

    async def _dispatch(self, frame: P.Frame, writer: asyncio.StreamWriter):
        dev, p = frame.device_id, frame.payload
        now = time.time()
        if isinstance(p, P.Hello):
            self.db.upsert_device(dev, hw_model=p.hw_model, fw_version=p.fw_string, fw_version_u32=frame.header.fw_version,
                                  telemetry_hz=p.telemetry_hz, reported_config_version=p.config_version,
                                  last_hello=now, last_seen=now)
            self._seen(dev)
            self.bus.publish("device", {"device_id": dev, "event": "hello", "fw_version": p.fw_string, "hw_model": p.hw_model,
                                        "config_version": p.config_version, "ts": iso(now)})
            self.db.add_log(dev, 1, f"HELLO fw={p.fw_string} hw={p.hw_model} cfg=v{p.config_version} hz={p.telemetry_hz}")
            await self._after_hello(dev, p)
        elif isinstance(p, P.Heartbeat):
            self.db.upsert_device(dev, uptime_s=p.uptime_s, device_reconnects=p.reconnects,
                                  reported_config_version=p.config_version, last_heartbeat=now, last_seen=now)
            self._seen(dev)
        elif isinstance(p, P.Log):
            row = self.db.add_log(dev, p.level, p.message, frame.header.ts_ms)
            row["level_name"] = p.level_name
            self.bus.publish("log", row)
            self._seen(dev)
        elif isinstance(p, P.ConfigAck):
            status = p.status_name
            self.db.ack_config(dev, p.config_version, status)
            if status == "applied":
                self.db.upsert_device(dev, reported_config_version=p.config_version)
            self.engine.invalidate_config(dev)
            self.bus.publish("device", {"device_id": dev, "event": "config_ack", "config_version": p.config_version,
                                        "status": status, "ts": iso(now)})
            self.db.add_log(dev, 1 if status == "applied" else 2, f"CONFIG_ACK v{p.config_version} {status}")
        elif isinstance(p, P.FwAck):
            job = self.db.fw_job_latest(dev)
            if job and job["status"] not in ("complete", "rejected", "failed"):
                self.db.fw_job_update(job["id"], p.status_name)
                job = self.db.fw_job_get(job["id"])
                self.bus.publish("device", {"device_id": dev, "event": "fw_job", "job": job, "ts": iso(now)})
            self.db.add_log(dev, 1, f"FW_ACK {p.status_name} {p.version}")
        elif isinstance(p, P.FaultAck):
            self.db.fault_injection_ack(dev, p.active_flags)
            self.bus.publish("device", {"device_id": dev, "event": "fault_ack", "active_flags": p.active_flags,
                                        "faults": P.flags_to_names(p.active_flags), "ts": iso(now)})
        elif isinstance(p, P.Telemetry):
            self.handle_telemetry(frame)

    async def _after_hello(self, dev: str, hello: P.Hello):
        cfg = self.db.current_config(dev)
        if cfg and cfg["version"] > hello.config_version:
            await self.push_config(dev, cfg)
        job = self.db.fw_job_latest(dev)
        if job and job["status"] not in ("complete", "rejected", "failed"):
            if hello.fw_string == job["target_version"]:
                self.db.fw_job_update(job["id"], "complete")
                self.bus.publish("device", {"device_id": dev, "event": "fw_job", "job": self.db.fw_job_get(job["id"])})
            elif job["status"] in ("pending", "accepted", "downloading"):
                fw = self.db.firmware_get(job["target_version"])
                if fw:
                    await self.send(dev, P.FwUpdate(fw["version"], fw["size_bytes"], fw["crc"]))

    # ------------------------------------------------------------ B->D pushes
    def is_connected(self, device_id: str) -> bool:
        return device_id in self.writers

    async def send(self, device_id: str, payload: P.Payload) -> bool:
        w = self.writers.get(device_id)
        if w is None or w.is_closing():
            return False
        s = self.st(device_id)
        s.tx_seq += 1
        dev = self.db.get_device(device_id) or {}
        frame = P.encode(payload.TYPE, device_id, s.tx_seq, int(time.time() * 1000), dev.get("fw_version_u32") or 0, payload)
        try:
            w.write(frame)
            await w.drain()
            return True
        except (ConnectionError, RuntimeError):
            return False

    async def push_config(self, device_id: str, cfg: dict) -> bool:
        ok = await self.send(device_id, P.ConfigSet(cfg["version"], int(cfg["telemetry_hz"]), int(cfg["log_level"]),
                                                    float(cfg["rpm_limit"]), float(cfg["temp_limit_c"]),
                                                    float(cfg["current_limit_a"])))
        self.db.x("UPDATE device_config SET ack_status=? WHERE device_id=? AND version=?",
                  ("sent" if ok else "queued", device_id, cfg["version"]))
        self.engine.invalidate_config(device_id)
        return ok

    async def push_fw(self, device_id: str, fw: dict) -> bool:
        return await self.send(device_id, P.FwUpdate(fw["version"], int(fw["size_bytes"]), int(fw["crc"])))

    async def push_fault(self, device_id: str, set_flags: int, clear_flags: int, duration_ms: int) -> bool:
        return await self.send(device_id, P.FaultInject(set_flags, clear_flags, duration_ms))

    async def push_cmd(self, device_id: str, cmd: int) -> bool:
        return await self.send(device_id, P.Cmd(cmd))

    # ------------------------------------------------------------ views
    def fleet_stats(self) -> dict:
        devs = self.stats
        losses = [s.loss_pct for s in devs.values() if s.frames_rx]
        return {"devices": len(self.db.list_devices()), "devices_online": sum(1 for s in devs.values() if s.online),
                "frames": sum(s.frames_rx for s in devs.values()), "frames_total_rx": self.total_frames,
                "bad_crc": sum(s.bad_crc for s in devs.values()) + self.global_bad_frames,
                "loss_pct": round(sum(losses) / len(losses), 2) if losses else 0.0,
                "tcp_connected": sum(1 for s in devs.values() if s.tcp_connected)}


class _UdpProto(asyncio.DatagramProtocol):
    def __init__(self, ingest: Ingest):
        self.ingest = ingest

    def datagram_received(self, data: bytes, addr):
        try:
            self.ingest.on_datagram(data)
        except Exception as e:  # pragma: no cover
            print("udp error:", repr(e))

    def error_received(self, exc):  # pragma: no cover
        print("udp error_received:", exc)
