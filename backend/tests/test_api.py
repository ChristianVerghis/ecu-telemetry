"""End-to-end: HTTP API + real UDP/TCP ingest on ephemeral ports + fake device."""
import asyncio
import socket
import time

import httpx
import pytest
import pytest_asyncio

from app import protocol as P
from app.main import create_app

DEV = "ECU-0007"


class FakeDevice:
    """Minimal TCP device: HELLO, heartbeats on demand, records everything the backend pushes."""

    def __init__(self, tcp_port, fw="1.0.0", config_version=0):
        self.tcp_port, self.fw, self.config_version = tcp_port, fw, config_version
        self.seq = 0
        self.received: list[P.Frame] = []
        self.dec = P.StreamDecoder()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.tcp_port)
        self._task = asyncio.create_task(self._read())
        await self.send(P.Hello(self.fw, "SIM-TEST", self.config_version, 10))

    async def _read(self):
        try:
            while True:
                data = await self.reader.read(4096)
                if not data:
                    return
                for f in self.dec.feed(data):
                    self.received.append(f)
                    if isinstance(f.payload, P.ConfigSet):
                        self.config_version = f.payload.config_version
                        await self.send(P.ConfigAck(f.payload.config_version, 0))
                    elif isinstance(f.payload, P.FaultInject):
                        await self.send(P.FaultAck(f.payload.set_flags & ~f.payload.clear_flags))
                    elif isinstance(f.payload, P.FwUpdate):
                        await self.send(P.FwAck(0, f.payload.target_version))
        except (asyncio.CancelledError, ConnectionError):
            pass

    async def send(self, payload):
        self.seq += 1
        self.writer.write(P.encode_frame(DEV, self.seq, int(time.time() * 1000), self.fw, payload))
        await self.writer.drain()

    async def wait_for(self, cls, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.received:
                if isinstance(f.payload, cls):
                    return f
            await asyncio.sleep(0.02)
        raise AssertionError(f"device never received {cls.__name__}")

    async def close(self):
        self._task.cancel()
        self.writer.close()


@pytest_asyncio.fixture
async def server(tmp_path):
    """Real uvicorn server on an ephemeral HTTP port (httpx's ASGITransport buffers responses, so SSE needs this)."""
    import uvicorn
    app = create_app(db_path=str(tmp_path / "api.db"), udp_port=0, tcp_port=0, host="127.0.0.1")
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
    srv = uvicorn.Server(config)
    srv.install_signal_handlers = lambda: None
    task = asyncio.create_task(srv.serve())
    for _ in range(200):
        if srv.started:
            break
        await asyncio.sleep(0.02)
    assert srv.started
    port = srv.servers[0].sockets[0].getsockname()[1]
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
        yield app, client
    srv.should_exit = True
    await asyncio.wait_for(task, 10)


def udp_send(port, frames):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for f in frames:
        s.sendto(f, ("127.0.0.1", port))
    s.close()


def tel(seq, **kw):
    t = P.Telemetry(rpm=3000, temp_c=60, current_a=40, voltage_v=96, vibration_g=0.3, speed_kph=30, lat=51.5, lon=-0.1, state=1)
    for k, v in kw.items():
        setattr(t, k, v)
    return P.encode_frame(DEV, seq, int(time.time() * 1000), "1.0.0", t)


async def settle(app, n=0.15):
    await asyncio.sleep(n)
    app.state.db.flush()


async def test_full_flow(server):
    app, client = server
    st = app.state
    udp_port, tcp_port = st.ingest.udp_port, st.ingest.tcp_port

    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert (await client.get("/api/devices")).json() == []
    assert (await client.get("/")).headers["content-type"].startswith("text/html")

    # --- UDP telemetry with a deliberate seq gap (1..10, 21..30 => 10 lost of 30 expected) + one bad-CRC frame
    frames = [tel(i) for i in range(1, 11)] + [tel(i) for i in range(21, 31)]
    bad = bytearray(tel(99)); bad[50] ^= 0xFF
    udp_send(udp_port, frames + [bytes(bad)])
    await settle(app, 0.3)
    devs = (await client.get("/api/devices")).json()
    assert len(devs) == 1 and devs[0]["device_id"] == DEV and devs[0]["online"] is True
    assert devs[0]["loss_pct"] > 0
    assert devs[0]["latest"]["rpm"] == 3000.0
    detail = (await client.get(f"/api/devices/{DEV}")).json()
    assert detail["stats"]["frames_rx"] == 20 and detail["stats"]["seq_gaps"] == 10 and detail["stats"]["bad_crc"] == 1
    assert detail["stats"]["loss_pct"] == pytest.approx(100 * 10 / 30, abs=0.1)
    # packet-loss alert fired (loss 33% > 10)
    alerts = (await client.get("/api/alerts", params={"device": DEV})).json()
    assert "packet_loss" in {a["rule"] for a in alerts}
    stats = (await client.get("/api/stats")).json()
    assert stats["frames"] == 20 and stats["bad_crc"] == 1 and stats["alerts_open"] >= 1

    rows = (await client.get(f"/api/devices/{DEV}/telemetry", params={"limit": 5})).json()
    assert rows["count"] == 5 and rows["rows"][-1]["seq"] == 30
    rows10 = (await client.get(f"/api/devices/{DEV}/telemetry", params={"step": "10s"})).json()
    assert rows10["step"] == "10s" and rows10["count"] >= 1

    # --- fake TCP device: HELLO + heartbeat
    dev = FakeDevice(tcp_port)
    await dev.connect()
    await dev.send(P.Heartbeat(uptime_s=12, frames_sent=30, config_version=0, reconnects=0))
    await settle(app)
    d = (await client.get(f"/api/devices/{DEV}")).json()
    assert d["tcp_connected"] is True and d["hw_model"] == "SIM-TEST" and d["fw_version"] == "1.0.0" and d["uptime_s"] == 12
    logs = (await client.get(f"/api/devices/{DEV}/logs")).json()
    assert any("HELLO" in l["message"] for l in logs)

    # LOG frame is stored
    await dev.send(P.Log(3, "motor controller fault code 0x12"))
    await settle(app)
    logs = (await client.get(f"/api/devices/{DEV}/logs")).json()
    assert logs[0]["message"] == "motor controller fault code 0x12" and logs[0]["level_name"] == "error"

    # --- PUT config -> CONFIG_SET pushed -> device ACKs -> version/ack visible
    r = await client.put(f"/api/devices/{DEV}/config",
                         json={"telemetry_hz": 20, "log_level": "warn", "rpm_limit": 5500, "temp_limit_c": 85, "current_limit_a": 110})
    assert r.status_code == 200 and r.json()["pushed"] is True and r.json()["version"] == 1
    f = await dev.wait_for(P.ConfigSet)
    assert f.payload.telemetry_hz == 20 and f.payload.log_level == 2 and f.payload.temp_limit_c == pytest.approx(85.0)
    await settle(app, 0.2)
    cfg = (await client.get(f"/api/devices/{DEV}/config")).json()
    assert cfg["version"] == 1 and cfg["ack_status"] == "applied" and cfg["log_level_name"] == "warn"

    # --- POST fault -> FAULT_INJECT pushed; FAULT_ACK recorded
    r = await client.post(f"/api/devices/{DEV}/fault", json={"set": ["OVERTEMP", "GPS_LOST"], "clear": [], "duration_ms": 5000})
    assert r.status_code == 202 and r.json()["set_flags"] == 0x21
    f = await dev.wait_for(P.FaultInject)
    assert f.payload.set_flags == 0x21 and f.payload.duration_ms == 5000
    await settle(app)
    d = (await client.get(f"/api/devices/{DEV}")).json()
    assert d["fault_injections"][0]["ack_flags"] == 0x21
    r = await client.post(f"/api/devices/{DEV}/fault", json={"set": ["BOGUS"]})
    assert r.status_code == 422

    # --- CMD
    r = await client.post(f"/api/devices/{DEV}/cmd", json={"cmd": "request_hello"})
    assert r.status_code == 202
    f = await dev.wait_for(P.Cmd)
    assert f.payload.cmd == 2

    # --- firmware registry + deploy -> FW_UPDATE pushed -> FW_ACK accepted -> job status
    r = await client.post("/api/firmware", json={"version": "1.1.0", "notes": "test", "size_bytes": 1_000_000})
    assert r.status_code == 201
    assert (await client.get("/api/firmware")).json()[0]["version"] == "1.1.0"
    r = await client.post(f"/api/devices/{DEV}/firmware", json={"version": "1.1.0"})
    assert r.status_code == 202 and r.json()["pushed"] is True
    f = await dev.wait_for(P.FwUpdate)
    assert f.payload.target_version == "1.1.0" and f.payload.image_size_bytes == 1_000_000
    await settle(app, 0.2)
    job = (await client.get(f"/api/devices/{DEV}/firmware/status")).json()
    assert job["status"] == "accepted" and [h["status"] for h in job["history"]][:3] == ["pending", "sent", "accepted"]
    # device "reboots" and reconnects with the new version -> job complete, reconnects counted
    await dev.close()
    dev2 = FakeDevice(tcp_port, fw="1.1.0", config_version=0)
    await dev2.connect()
    await settle(app, 0.3)
    job = (await client.get(f"/api/devices/{DEV}/firmware/status")).json()
    assert job["status"] == "complete"
    d = (await client.get(f"/api/devices/{DEV}")).json()
    assert d["fw_version"] == "1.1.0" and d["stats"]["reconnects"] >= 1
    # stored config v1 is newer than HELLO's v0 -> CONFIG_SET pushed automatically on connect
    f = await dev2.wait_for(P.ConfigSet)
    assert f.payload.config_version == 1

    # --- telemetry over threshold -> alerts; ack an alert
    udp_send(udp_port, [tel(31 + i, temp_c=120.0, fault_flags=0x01) for i in range(3)])
    await settle(app, 0.3)
    alerts = (await client.get("/api/alerts", params={"device": DEV, "status": "open"})).json()
    rules = {a["rule"] for a in alerts}
    assert {"temp_high", "temp_critical", "flag:OVERTEMP"} <= rules
    aid = alerts[0]["id"]
    r = await client.post(f"/api/alerts/{aid}/ack")
    assert r.json()["status"] == "acked"
    assert (await client.post("/api/alerts/999999/ack")).status_code == 404

    # --- rules
    rules_doc = (await client.get("/api/rules")).json()
    assert "thresholds" in rules_doc
    r = await client.put("/api/rules", json={"comm": {"loss_pct": 50}})
    assert r.json()["comm"]["loss_pct"] == 50

    # --- heuristic diagnose (no API key)
    r = await client.post(f"/api/devices/{DEV}/diagnose")
    assert r.status_code == 200
    dg = r.json()
    assert dg["model"] == "heuristic" and 0 <= dg["confidence"] <= 1
    assert isinstance(dg["probable_causes"], list) and dg["probable_causes"][0]["cause"]
    assert isinstance(dg["recommended_actions"], list) and dg["summary"]
    assert any("OVERTEMP" in c["cause"] or "temp" in c["cause"].lower() for c in dg["probable_causes"])
    assert (await client.get(f"/api/devices/{DEV}/diagnoses")).json()[0]["model"] == "heuristic"

    # --- 404s
    assert (await client.get("/api/devices/NOPE")).status_code == 404
    assert (await client.post("/api/devices/NOPE/diagnose")).status_code == 404
    await dev2.close()


async def test_sse_streams_events(server):
    app, client = server
    st = app.state
    got = []

    async def consume():
        async with client.stream("GET", "/api/events/stream") as resp:
            assert resp.headers["content-type"].startswith("text/event-stream")
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    got.append(line.split(":", 1)[1].strip())
                if len(got) >= 3:
                    return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    udp_send(st.ingest.udp_port, [tel(1), tel(2, temp_c=130.0)])
    await asyncio.wait_for(task, 5)
    assert got[0] == "hello" and "telemetry" in got and ("device" in got or "alert" in got)


async def test_offline_detection(server, monkeypatch):
    app, client = server
    st = app.state
    from app import ingest as ing
    monkeypatch.setattr(ing, "OFFLINE_AFTER_S", 0.3)
    udp_send(st.ingest.udp_port, [tel(1)])
    await asyncio.sleep(0.15)
    assert (await client.get("/api/devices")).json()[0]["online"] is True
    await asyncio.sleep(1.6)
    d = (await client.get("/api/devices")).json()[0]
    assert d["online"] is False
    alerts = (await client.get("/api/alerts", params={"device": DEV})).json()
    assert "offline" in {a["rule"] for a in alerts}
    udp_send(st.ingest.udp_port, [tel(2)])
    await asyncio.sleep(0.15)
    assert (await client.get("/api/devices")).json()[0]["online"] is True
    assert "offline" not in {a["rule"] for a in (await client.get("/api/alerts", params={"device": DEV})).json()}
