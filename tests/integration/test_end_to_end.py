"""End-to-end: real firmware agent -> real backend, checked over the HTTP API.

Runs one agent for ~8 s with an OVERTEMP fault at t=2 s for 5 s and 10 %
agent-side UDP loss, then asserts what the backend reports. Skips cleanly when
cmake or the backend venv is missing (see tests/conftest.py).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from conftest import terminate, wait_until  # noqa: E402

DEVICE = "ECU-IT01"
RUN_S = 8


def _fw_str(v):
    if isinstance(v, int):
        return f"{(v >> 16) & 0xFFFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"
    return str(v)


def _alerts(client: httpx.Client, status="all") -> list[dict]:
    r = client.get("/api/alerts", params={"device": DEVICE, "status": status, "limit": 200})
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):
        data = data.get("alerts") or data.get("items") or []
    return data


def _device(client: httpx.Client) -> dict | None:
    r = client.get(f"/api/devices/{DEVICE}")
    return r.json() if r.status_code == 200 else None


def test_agent_backend_end_to_end(agent_binary, backend, tmp_path):
    base = backend["base"]
    agent_log = open(tmp_path / "agent.log", "wb")
    agent = subprocess.Popen(
        [str(agent_binary), "--id", DEVICE, "--host", "127.0.0.1",
         "--udp-port", str(backend["udp_port"]), "--tcp-port", str(backend["tcp_port"]),
         "--hz", "10", "--profile", "highway", "--seed", "7", "--fw", "1.0.0",
         "--drop-rate", "0.1", "--fault", "OVERTEMP@2:5", "--duration", str(RUN_S),
         "--log-level", "info"],
        stdout=agent_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        with httpx.Client(base_url=base, timeout=5.0) as c:
            # 1. device appears online with telemetry
            dev = wait_until(
                lambda: (d := _device(c)) and d.get("online") and (d.get("latest") or d.get("latest_telemetry")) and d,
                timeout=15, what="device online with telemetry",
            )
            assert dev["device_id"] == DEVICE
            assert _fw_str(dev.get("fw_version")) == "1.0.0"
            latest = dev.get("latest") or dev.get("latest_telemetry")
            assert "rpm" in latest and "temp_c" in latest

            # 2. an OVERTEMP alert opened while the fault is active
            wait_until(
                lambda: [a for a in _alerts(c) if "overtemp" in str(a).lower()],
                timeout=12, what="OVERTEMP alert",
            )

            # 3. config PUT bumps the version
            before = (_device(c) or {}).get("config_version") or 0
            r = c.put(f"/api/devices/{DEVICE}/config", json={
                "telemetry_hz": 20, "log_level": "info", "rpm_limit": 9000.0,
                "temp_limit_c": 110.0, "current_limit_a": 400.0,
            })
            assert r.status_code in (200, 201, 202), r.text
            body = r.json()
            new_version = body.get("config_version") or body.get("version") or (body.get("config") or {}).get("version")
            assert new_version is not None and int(new_version) > int(before), body
            wait_until(lambda: int((_device(c) or {}).get("config_version") or 0) >= int(new_version),
                       timeout=5, what="device record shows bumped config_version")

            # let the agent finish its run so loss stats and logs are settled
            try:
                agent.wait(RUN_S + 5)
            except subprocess.TimeoutExpired:
                pass

            # 4. loss_pct > 0 (10 % simulated drop)
            dev = _device(c)
            assert dev is not None
            loss = dev.get("loss_pct")
            if loss is None and isinstance(dev.get("stats"), dict):
                loss = dev["stats"].get("loss_pct")
            assert loss is not None, f"loss_pct missing from device record: {dev}"
            assert float(loss) > 0.0, f"expected packet loss > 0, got {loss}"

            # 5. logs non-empty (LOG frames over TCP)
            r = c.get(f"/api/devices/{DEVICE}/logs", params={"limit": 200})
            assert r.status_code == 200
            logs = r.json()
            if isinstance(logs, dict):
                logs = logs.get("logs") or logs.get("items") or []
            assert len(logs) > 0, "expected at least one LOG frame to be stored"

            # bonus: fleet stats reflect the device
            stats = c.get("/api/stats").json()
            assert stats.get("devices", 1) >= 1
    finally:
        terminate(agent)
        agent_log.close()
    assert agent.returncode in (0, -15), f"agent rc={agent.returncode}\n{(tmp_path / 'agent.log').read_text(errors='replace')[-2000:]}"
