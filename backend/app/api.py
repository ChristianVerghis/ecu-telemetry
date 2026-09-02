"""HTTP API + SSE. Everything from docs/interfaces.md."""
from __future__ import annotations

import asyncio
import json
import time
import zlib
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import protocol as P
from .db import iso, parse_iso

router = APIRouter()


def _s(request: Request):
    return request.app.state


def _device_or_404(st, device_id: str) -> dict:
    d = st.db.get_device(device_id)
    if not d:
        raise HTTPException(404, f"unknown device {device_id}")
    return d


def _device_view(st, d: dict, open_counts: dict | None = None) -> dict:
    dev = d["device_id"]
    s = st.ingest.st(dev)
    latest = st.ingest.latest.get(dev) or st.db.latest_telemetry(dev)
    cfg = st.db.current_config(dev)
    if open_counts is None:
        open_counts = st.db.open_alert_counts()
    return {
        "device_id": dev, "state": d.get("state"), "state_name": P.STATE_NAMES.get(d.get("state") or 0),
        "online": bool(s.online if dev in st.ingest.stats else d.get("online")),
        "tcp_connected": s.tcp_connected, "last_seen": iso(s.last_seen or d.get("last_seen")),
        "fw_version": d.get("fw_version"), "hw_model": d.get("hw_model"),
        "config_version": cfg["version"] if cfg else (d.get("reported_config_version") or 0),
        "reported_config_version": d.get("reported_config_version") or 0,
        "telemetry_hz": d.get("telemetry_hz"), "uptime_s": d.get("uptime_s"),
        "loss_pct": s.loss_pct, "frames_rx": s.frames_rx, "bad_crc": s.bad_crc, "reconnects": s.reconnects,
        "latest": {k: latest.get(k) for k in ("rpm", "temp_c", "current_a", "voltage_v", "vibration_g", "speed_kph",
                                              "lat", "lon", "fault_flags", "state", "ts_iso")} if latest else None,
        "faults": P.flags_to_names(int((latest or {}).get("fault_flags") or 0)),
        "open_alerts": open_counts.get(dev, 0),
    }


# ---------------------------------------------------------------- health / stats

@router.get("/health")
async def health(request: Request):
    st = _s(request)
    return {"status": "ok", "devices_online": sum(1 for s in st.ingest.stats.values() if s.online),
            "uptime_s": round(time.time() - st.started_at, 1), "udp_port": st.ingest.udp_port, "tcp_port": st.ingest.tcp_port}


@router.get("/api/stats")
async def stats(request: Request):
    st = _s(request)
    out = st.ingest.fleet_stats()
    out["alerts_open"] = sum(st.db.open_alert_counts().values())
    out["sse_clients"] = len(st.bus.subs)
    out["uptime_s"] = round(time.time() - st.started_at, 1)
    return out


# ---------------------------------------------------------------- devices

@router.get("/api/devices")
async def devices(request: Request):
    st = _s(request)
    counts = st.db.open_alert_counts()
    return [_device_view(st, d, counts) for d in st.db.list_devices()]


@router.get("/api/devices/{device_id}")
async def device_detail(request: Request, device_id: str):
    st = _s(request)
    d = _device_or_404(st, device_id)
    v = _device_view(st, d)
    v["stats"] = st.ingest.st(device_id).as_dict()
    v["config"] = st.db.current_config(device_id)
    v["config_history"] = st.db.config_history(device_id, 10)
    v["fw_job"] = st.db.fw_job_latest(device_id)
    v["fault_injections"] = st.db.fault_injections(device_id, 10)
    v["alerts"] = st.db.alerts(device_id, "open", 50)
    v["diagnoses"] = st.db.diagnoses(device_id, 3)
    v["first_seen"] = iso(d.get("first_seen"))
    v["last_hello"] = iso(d.get("last_hello"))
    return v


@router.get("/api/devices/{device_id}/telemetry")
async def device_telemetry(request: Request, device_id: str, since: str | None = None, until: str | None = None,
                           limit: int = Query(2000, ge=1, le=20000), step: str = "raw"):
    st = _s(request)
    _device_or_404(st, device_id)
    step = {"1s": "raw", "raw": "raw", "10s": "10s", "1m": "1m", "60s": "1m"}.get(step, "raw")
    await asyncio.to_thread(st.db.flush)
    rows = await asyncio.to_thread(st.db.telemetry, device_id, parse_iso(since), parse_iso(until), limit, step)
    return {"device_id": device_id, "step": step, "count": len(rows), "rows": rows}


@router.get("/api/devices/{device_id}/logs")
async def device_logs(request: Request, device_id: str, limit: int = Query(200, ge=1, le=5000)):
    st = _s(request)
    _device_or_404(st, device_id)
    rows = st.db.logs(device_id, limit)
    for r in rows:
        r["level_name"] = P.LOG_LEVELS.get(r["level"], str(r["level"]))
    return rows


# ---------------------------------------------------------------- config

class ConfigBody(BaseModel):
    telemetry_hz: int = Field(10, ge=1, le=100)
    log_level: int | str = 1
    rpm_limit: float = Field(6000.0, ge=0)
    temp_limit_c: float = Field(90.0, ge=-40, le=300)
    current_limit_a: float = Field(120.0, ge=0)


def _log_level(v) -> int:
    if isinstance(v, int):
        if v not in P.LOG_LEVELS:
            raise HTTPException(422, "log_level must be 0..3")
        return v
    key = str(v).lower()
    if key not in P.LOG_LEVEL_IDS:
        raise HTTPException(422, f"log_level must be one of {list(P.LOG_LEVEL_IDS)}")
    return P.LOG_LEVEL_IDS[key]


def _config_view(st, device_id: str, cfg: dict | None) -> dict:
    d = st.db.get_device(device_id) or {}
    if not cfg:
        return {"device_id": device_id, "version": d.get("reported_config_version") or 0, "source": "device-default",
                "ack_status": None, **{k: None for k in ("telemetry_hz", "log_level", "rpm_limit", "temp_limit_c", "current_limit_a")},
                "reported_config_version": d.get("reported_config_version") or 0}
    return {**cfg, "log_level_name": P.LOG_LEVELS.get(cfg["log_level"]), "created_at_iso": iso(cfg["created_at"]),
            "acked_at_iso": iso(cfg.get("acked_at")), "reported_config_version": d.get("reported_config_version") or 0,
            "source": "backend"}


@router.get("/api/devices/{device_id}/config")
async def get_config(request: Request, device_id: str):
    st = _s(request)
    _device_or_404(st, device_id)
    return _config_view(st, device_id, st.db.current_config(device_id))


@router.put("/api/devices/{device_id}/config")
async def put_config(request: Request, device_id: str, body: ConfigBody):
    st = _s(request)
    _device_or_404(st, device_id)
    cfg = st.db.new_config(device_id, body.telemetry_hz, _log_level(body.log_level), body.rpm_limit,
                           body.temp_limit_c, body.current_limit_a)
    pushed = await st.ingest.push_config(device_id, cfg)
    cfg = st.db.current_config(device_id)
    st.db.add_log(device_id, 1, f"CONFIG_SET v{cfg['version']} {'pushed' if pushed else 'queued (device not connected)'}")
    view = _config_view(st, device_id, cfg)
    view["pushed"] = pushed
    st.bus.publish("device", {"device_id": device_id, "event": "config_set", "config_version": cfg["version"],
                              "pushed": pushed, "ts": iso(time.time())})
    return view


# ---------------------------------------------------------------- firmware

class FirmwareBody(BaseModel):
    version: str
    notes: str = ""
    size_bytes: int = Field(1_000_000, ge=1)


@router.get("/api/firmware")
async def firmware_list(request: Request):
    return _s(request).db.firmware_list()


@router.post("/api/firmware", status_code=201)
async def firmware_add(request: Request, body: FirmwareBody):
    st = _s(request)
    try:
        P.fw_encode(body.version)
    except P.ProtocolError as e:
        raise HTTPException(422, str(e))
    crc = zlib.crc32(f"fw:{body.version}:{body.size_bytes}".encode()) & 0xFFFFFFFF
    fw = st.db.firmware_add(body.version, body.notes, body.size_bytes, crc)
    fw["created_at_iso"] = iso(fw["created_at"])
    return fw


class FwDeployBody(BaseModel):
    version: str


@router.post("/api/devices/{device_id}/firmware", status_code=202)
async def firmware_deploy(request: Request, device_id: str, body: FwDeployBody):
    st = _s(request)
    d = _device_or_404(st, device_id)
    fw = st.db.firmware_get(body.version)
    if not fw:
        raise HTTPException(404, f"firmware {body.version} not in registry")
    job = st.db.fw_job_create(device_id, body.version, d.get("fw_version"))
    pushed = await st.ingest.push_fw(device_id, fw)
    st.db.fw_job_update(job["id"], "sent" if pushed else "queued")
    job = st.db.fw_job_get(job["id"])
    st.db.add_log(device_id, 1, f"FW_UPDATE -> {body.version} {'pushed' if pushed else 'queued'}")
    st.bus.publish("device", {"device_id": device_id, "event": "fw_job", "job": job, "ts": iso(time.time())})
    return {"job_id": job["id"], "pushed": pushed, **job}


@router.get("/api/devices/{device_id}/firmware/status")
async def firmware_status(request: Request, device_id: str):
    st = _s(request)
    _device_or_404(st, device_id)
    job = st.db.fw_job_latest(device_id)
    return job or {"device_id": device_id, "status": "none"}


# ---------------------------------------------------------------- fault / cmd

class FaultBody(BaseModel):
    set: list[str] = []
    clear: list[str] = []
    duration_ms: int = Field(0, ge=0)


@router.post("/api/devices/{device_id}/fault", status_code=202)
async def fault_inject(request: Request, device_id: str, body: FaultBody):
    st = _s(request)
    _device_or_404(st, device_id)
    try:
        set_flags, clear_flags = P.names_to_flags(body.set), P.names_to_flags(body.clear)
    except P.ProtocolError as e:
        raise HTTPException(422, str(e))
    fid = st.db.fault_injection_add(device_id, set_flags, clear_flags, body.duration_ms)
    pushed = await st.ingest.push_fault(device_id, set_flags, clear_flags, body.duration_ms)
    st.db.add_log(device_id, 2, f"FAULT_INJECT set={P.flags_to_names(set_flags)} clear={P.flags_to_names(clear_flags)} "
                                f"dur={body.duration_ms}ms {'pushed' if pushed else 'NOT connected'}")
    if not pushed:
        raise HTTPException(409, "device control channel not connected")
    return {"id": fid, "device_id": device_id, "set": P.flags_to_names(set_flags), "clear": P.flags_to_names(clear_flags),
            "set_flags": set_flags, "clear_flags": clear_flags, "duration_ms": body.duration_ms, "pushed": True}


class CmdBody(BaseModel):
    cmd: Literal["reboot", "clear_faults", "request_hello"]


@router.post("/api/devices/{device_id}/cmd", status_code=202)
async def device_cmd(request: Request, device_id: str, body: CmdBody):
    st = _s(request)
    _device_or_404(st, device_id)
    pushed = await st.ingest.push_cmd(device_id, P.CMD_IDS[body.cmd])
    st.db.add_log(device_id, 1, f"CMD {body.cmd} {'pushed' if pushed else 'NOT connected'}")
    if not pushed:
        raise HTTPException(409, "device control channel not connected")
    return {"device_id": device_id, "cmd": body.cmd, "pushed": True}


# ---------------------------------------------------------------- alerts / rules

@router.get("/api/alerts")
async def alerts(request: Request, device: str | None = None, status: str = "open", limit: int = Query(200, ge=1, le=5000)):
    return _s(request).db.alerts(device, status, limit)


@router.post("/api/alerts/{alert_id}/ack")
async def ack_alert(request: Request, alert_id: int):
    st = _s(request)
    a = st.db.get_alert(alert_id)
    if not a:
        raise HTTPException(404, "no such alert")
    if a["status"] == "open":
        st.db.update_alert(alert_id, status="acked", acked_at=time.time())
    a = st.db.get_alert(alert_id)
    st.bus.publish("alert", {"action": "acked", **a})
    return a


@router.get("/api/rules")
async def get_rules(request: Request):
    return _s(request).engine.rules


@router.put("/api/rules")
async def put_rules(request: Request):
    st = _s(request)
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(422, "body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(422, "rules must be a JSON object")
    return st.engine.set_rules(body)


# ---------------------------------------------------------------- diagnose

@router.post("/api/devices/{device_id}/diagnose")
async def diagnose(request: Request, device_id: str):
    st = _s(request)
    _device_or_404(st, device_id)
    await asyncio.to_thread(st.db.flush)
    return await st.diag.diagnose(device_id)


@router.get("/api/devices/{device_id}/diagnoses")
async def diagnoses(request: Request, device_id: str, limit: int = 10):
    return _s(request).db.diagnoses(device_id, limit)


# ---------------------------------------------------------------- SSE

@router.get("/api/events/stream")
async def events_stream(request: Request):
    st = _s(request)
    q = st.bus.subscribe()

    async def gen():
        try:
            yield f"event: hello\ndata: {json.dumps({'ts': iso(time.time()), 'devices_online': sum(1 for s in st.ingest.stats.values() if s.online)})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev, data = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {ev}\ndata: {json.dumps(data, default=str)}\n\n"
        finally:
            st.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
