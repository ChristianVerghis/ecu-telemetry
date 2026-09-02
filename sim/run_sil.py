#!/usr/bin/env python3
"""SIL (software-in-the-loop) scenario runner for the ECU telemetry platform.

Reads a scenario YAML (see sim/README.md for the schema), spawns one firmware
agent process per device, optionally starts the backend and the UDP chaos
proxy, drives the timeline through the backend HTTP API and evaluates the
scenario's expectations at their scheduled times (each polled within a grace
window). Prints a coloured table plus writes a JSON report to
sim/reports/<scenario>-<timestamp>.json. Exit code 0 when every expectation
passed, 1 otherwise, 2 on usage / validation errors.

Dependencies: Python 3.13 stdlib + pyyaml + httpx.

Examples:
    python sim/run_sil.py sim/scenarios/thermal_runaway.yml --start-backend
    python sim/run_sil.py sim/scenarios/lossy_link.yml --backend http://localhost:8780
    python sim/run_sil.py --validate sim/scenarios/*.yml
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    print("run_sil.py needs pyyaml (pip install pyyaml httpx)", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SIM_DIR = Path(__file__).resolve().parent
REPORT_DIR = SIM_DIR / "reports"
DEFAULT_AGENT = REPO_ROOT / "firmware" / "build" / "ecu_agent"
GRACE_S = 5.0
POLL_S = 0.5

FAULT_NAMES = {
    "OVERTEMP": 0x01, "OVERCURRENT": 0x02, "UNDERVOLTAGE": 0x04, "VIBRATION_HIGH": 0x08,
    "SENSOR_STUCK": 0x10, "GPS_LOST": 0x20, "COMM_DEGRADED": 0x40, "ENCODER_FAULT": 0x80,
}
STATE_NAMES = {0: "IDLE", 1: "RUNNING", 2: "DEGRADED", 3: "FAULT"}
PROFILES = {"city", "highway", "idle", "stress"}
ACTIONS = {"fault", "clear_fault", "config", "firmware_register", "firmware_deploy", "cmd", "proxy_set"}
CHECKS = {
    "alert_open", "alert_resolved", "device_state", "device_fw", "config_version",
    "loss_pct_gt", "diagnosis_nonempty", "no_alerts_of_severity",
}
SEVERITIES = {"info", "warning", "critical"}
DEVICE_ACTIONS = {"fault", "clear_fault", "config", "firmware_deploy", "cmd"}
DEVICE_CHECKS = {"alert_open", "alert_resolved", "device_state", "device_fw", "config_version",
                 "loss_pct_gt", "diagnosis_nonempty"}


# --------------------------------------------------------------------------- #
# ANSI colours
# --------------------------------------------------------------------------- #
class C:
    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls.enabled else s

    @classmethod
    def green(cls, s): return cls._w("32", s)
    @classmethod
    def red(cls, s): return cls._w("31", s)
    @classmethod
    def yellow(cls, s): return cls._w("33", s)
    @classmethod
    def cyan(cls, s): return cls._w("36", s)
    @classmethod
    def dim(cls, s): return cls._w("2", s)
    @classmethod
    def bold(cls, s): return cls._w("1", s)


def log(msg: str, t0: float | None = None):
    stamp = f"[{time.time() - t0:6.1f}s] " if t0 is not None else ""
    print(C.dim(stamp) + msg, flush=True)


# --------------------------------------------------------------------------- #
# Scenario loading + validation
# --------------------------------------------------------------------------- #
class SchemaError(Exception):
    pass


def _require(cond: bool, msg: str):
    if not cond:
        raise SchemaError(msg)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def parse_fault_spec(spec: str) -> tuple[str, float, float]:
    """'OVERTEMP@20:40' -> ('OVERTEMP', 20.0, 40.0)"""
    _require(isinstance(spec, str) and "@" in spec and ":" in spec.split("@", 1)[1],
             f"fault spec {spec!r} must look like FLAG@START_S:DUR_S")
    name, rest = spec.split("@", 1)
    start, dur = rest.split(":", 1)
    _require(name in FAULT_NAMES, f"unknown fault flag {name!r} in {spec!r}")
    try:
        return name, float(start), float(dur)
    except ValueError:
        raise SchemaError(f"non-numeric start/duration in fault spec {spec!r}")


def validate_scenario(sc: dict) -> list[str]:
    """Return a list of warnings; raise SchemaError on hard errors."""
    warnings: list[str] = []
    _require(isinstance(sc, dict), "scenario must be a mapping")
    for key in ("name", "description", "duration_s", "backend", "devices", "timeline", "expect"):
        _require(key in sc, f"missing top-level key {key!r}")
    _require(isinstance(sc["name"], str) and sc["name"], "name must be a non-empty string")
    _require(_is_num(sc["duration_s"]) and sc["duration_s"] > 0, "duration_s must be a positive number")

    be = sc["backend"]
    _require(isinstance(be, dict), "backend must be a mapping")
    for k in ("http", "udp_port", "tcp_port"):
        _require(k in be, f"backend.{k} missing")
    _require(isinstance(be["http"], str) and be["http"].startswith("http"), "backend.http must be a URL")
    for k in ("udp_port", "tcp_port"):
        _require(isinstance(be[k], int) and 0 < be[k] < 65536, f"backend.{k} must be a port")

    if "proxy" in sc:
        px = sc["proxy"]
        _require(isinstance(px, dict) and isinstance(px.get("listen"), int), "proxy.listen (int port) required")
        for k in ("drop", "reorder", "dup"):
            if k in px:
                _require(_is_num(px[k]) and 0 <= px[k] <= 1, f"proxy.{k} must be in [0,1]")

    devs = sc["devices"]
    _require(isinstance(devs, list) and devs, "devices must be a non-empty list")
    ids: set[str] = set()
    for i, d in enumerate(devs):
        _require(isinstance(d, dict) and "id" in d, f"devices[{i}] needs an id")
        did = d["id"]
        _require(isinstance(did, str) and 0 < len(did) <= 12 and did.isascii(),
                 f"device id {did!r} must be ASCII, 1..12 chars (protocol char[12])")
        _require(did not in ids, f"duplicate device id {did!r}")
        ids.add(did)
        _require(d.get("profile", "city") in PROFILES, f"{did}: profile must be one of {sorted(PROFILES)}")
        fw = str(d.get("fw", "1.0.0"))
        _require(len(fw.split(".")) == 3 and all(p.isdigit() for p in fw.split(".")),
                 f"{did}: fw must be MAJOR.MINOR.PATCH")
        hz = d.get("hz", 10)
        _require(isinstance(hz, int) and 1 <= hz <= 100, f"{did}: hz must be int 1..100")
        dr = d.get("drop_rate", 0.0)
        _require(_is_num(dr) and 0 <= dr <= 1, f"{did}: drop_rate must be in [0,1]")
        _require(isinstance(d.get("seed", 1), int), f"{did}: seed must be int")
        for spec in d.get("faults", []) or []:
            name, start, dur = parse_fault_spec(spec)
            if start + dur > sc["duration_s"]:
                warnings.append(f"{did}: fault {spec} extends past duration_s")
        if d.get("via_proxy") and "proxy" not in sc:
            warnings.append(f"{did}: via_proxy set but scenario has no 'proxy' block (needs --via-proxy/--proxy-port)")

    tl = sc["timeline"]
    _require(isinstance(tl, list), "timeline must be a list")
    for i, ev in enumerate(tl):
        _require(isinstance(ev, dict) and "at_s" in ev and "action" in ev, f"timeline[{i}] needs at_s and action")
        _require(_is_num(ev["at_s"]) and ev["at_s"] >= 0, f"timeline[{i}].at_s must be >= 0")
        _require(ev["action"] in ACTIONS, f"timeline[{i}].action {ev['action']!r} not in {sorted(ACTIONS)}")
        params = ev.get("params", {}) or {}
        _require(isinstance(params, dict), f"timeline[{i}].params must be a mapping")
        if ev["action"] in DEVICE_ACTIONS:
            _require(ev.get("device") in ids, f"timeline[{i}] ({ev['action']}) device {ev.get('device')!r} not in devices")
        if ev["action"] == "fault":
            _require(isinstance(params.get("set"), list) and params["set"], f"timeline[{i}] fault needs params.set list")
            for n in params["set"]:
                _require(n in FAULT_NAMES, f"timeline[{i}] unknown fault {n!r}")
        if ev["action"] == "clear_fault":
            _require(isinstance(params.get("clear"), list) and params["clear"], f"timeline[{i}] clear_fault needs params.clear list")
        if ev["action"] == "config":
            for k in ("telemetry_hz", "log_level", "rpm_limit", "temp_limit_c", "current_limit_a"):
                _require(k in params, f"timeline[{i}] config needs params.{k}")
        if ev["action"] in ("firmware_register", "firmware_deploy"):
            _require(isinstance(params.get("version"), str), f"timeline[{i}] needs params.version")
        if ev["action"] == "cmd":
            _require(params.get("cmd") in ("reboot", "clear_faults", "request_hello"),
                     f"timeline[{i}] cmd must be reboot|clear_faults|request_hello")
        if ev["action"] == "proxy_set":
            _require("proxy" in sc, f"timeline[{i}] proxy_set requires a top-level 'proxy' block")
        if ev["at_s"] > sc["duration_s"]:
            warnings.append(f"timeline[{i}] at_s {ev['at_s']} is after duration_s")

    ex = sc["expect"]
    _require(isinstance(ex, list) and ex, "expect must be a non-empty list")
    for i, e in enumerate(ex):
        _require(isinstance(e, dict) and "at_s" in e and "check" in e, f"expect[{i}] needs at_s and check")
        _require(_is_num(e["at_s"]) and e["at_s"] >= 0, f"expect[{i}].at_s must be >= 0")
        _require(e["check"] in CHECKS, f"expect[{i}].check {e['check']!r} not in {sorted(CHECKS)}")
        params = e.get("params", {}) or {}
        _require(isinstance(params, dict), f"expect[{i}].params must be a mapping")
        if e["check"] in DEVICE_CHECKS:
            _require(e.get("device") in ids, f"expect[{i}] ({e['check']}) device {e.get('device')!r} not in devices")
        if e["check"] in ("alert_open", "alert_resolved"):
            _require("match" in params, f"expect[{i}] {e['check']} needs params.match (string or list)")
        if e["check"] == "device_fw":
            _require(isinstance(params.get("version"), str), f"expect[{i}] device_fw needs params.version")
        if e["check"] == "loss_pct_gt":
            _require(_is_num(params.get("value")), f"expect[{i}] loss_pct_gt needs numeric params.value")
        if e["check"] == "no_alerts_of_severity":
            _require(params.get("severity") in SEVERITIES, f"expect[{i}] severity must be one of {sorted(SEVERITIES)}")
        if e["check"] == "device_state":
            st = params.get("state")
            if st is not None:
                for s in ([st] if isinstance(st, str) else st):
                    _require(s in STATE_NAMES.values(), f"expect[{i}] unknown state {s!r}")
        if e["at_s"] > sc["duration_s"]:
            warnings.append(f"expect[{i}] at_s {e['at_s']} is after duration_s (will still be evaluated)")
    return warnings


def load_scenario(path: Path) -> dict:
    with open(path) as f:
        sc = yaml.safe_load(f)
    validate_scenario(sc)
    return sc


# --------------------------------------------------------------------------- #
# Backend API client (thin httpx wrapper, tolerant of missing fields)
# --------------------------------------------------------------------------- #
class Api:
    def __init__(self, base: str, timeout: float = 5.0):
        import httpx  # local import so --validate works without httpx
        self.base = base.rstrip("/")
        self.c = httpx.Client(base_url=self.base, timeout=timeout)

    def _json(self, r):
        try:
            return r.json()
        except ValueError:
            return None

    def get(self, path: str, **params):
        r = self.c.get(path, params=params or None)
        r.raise_for_status()
        return self._json(r)

    def post(self, path: str, body: dict | None = None):
        r = self.c.post(path, json=body if body is not None else {})
        r.raise_for_status()
        return self._json(r)

    def put(self, path: str, body: dict):
        r = self.c.put(path, json=body)
        r.raise_for_status()
        return self._json(r)

    def health_ok(self) -> bool:
        try:
            r = self.c.get("/health")
            return r.status_code == 200
        except Exception:
            return False

    def device(self, did: str) -> dict | None:
        try:
            return self.get(f"/api/devices/{did}")
        except Exception:
            return None

    def alerts(self, device: str | None = None, status: str = "all") -> list[dict]:
        params: dict[str, Any] = {"status": status, "limit": 500}
        if device:
            params["device"] = device
        try:
            data = self.get("/api/alerts", **params)
        except Exception:
            return []
        if isinstance(data, dict):
            data = data.get("alerts") or data.get("items") or []
        return [a for a in data if isinstance(a, dict)]

    def close(self):
        self.c.close()


# --------------------------------------------------------------------------- #
# Helpers to normalise backend responses
# --------------------------------------------------------------------------- #
def norm_state(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, int):
        return STATE_NAMES.get(v, str(v))
    return str(v).upper()


def fw_str(v) -> str | None:
    """fw_version may be '1.1.0' or the packed u32 (major<<16 | minor<<8 | patch)."""
    if v is None:
        return None
    if isinstance(v, int):
        return f"{(v >> 16) & 0xFFFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"
    return str(v).strip()


def alert_text(a: dict) -> str:
    return json.dumps(a, default=str).lower()


def alert_is_open(a: dict) -> bool:
    st = str(a.get("status", "")).lower()
    if st:
        return st == "open"
    return a.get("resolved_at") is None and not a.get("resolved", False)


def alert_matches(a: dict, match) -> bool:
    text = alert_text(a)
    needles = [match] if isinstance(match, str) else list(match)
    return any(str(n).lower() in text for n in needles)


def alert_device(a: dict) -> str | None:
    return a.get("device_id") or a.get("device")


def fault_names_from(v) -> set[str]:
    if v is None:
        return set()
    if isinstance(v, int):
        return {n for n, bit in FAULT_NAMES.items() if v & bit}
    if isinstance(v, str):
        return {v.upper()}
    return {str(x).upper() for x in v}


def latest_of(dev: dict) -> dict:
    lat = dev.get("latest") or dev.get("latest_telemetry") or dev.get("telemetry") or {}
    return lat if isinstance(lat, dict) else {}


def loss_pct_of(dev: dict) -> float | None:
    for k in ("loss_pct", "packet_loss_pct", "loss"):
        if k in dev and dev[k] is not None:
            return float(dev[k])
    stats = dev.get("stats") or {}
    if isinstance(stats, dict) and stats.get("loss_pct") is not None:
        return float(stats["loss_pct"])
    return None


def config_version_of(dev: dict) -> int | None:
    v = dev.get("config_version")
    if v is None and isinstance(dev.get("config"), dict):
        v = dev["config"].get("version") or dev["config"].get("config_version")
    return int(v) if v is not None else None


def config_acked(dev: dict) -> bool | None:
    """Best-effort: backend may expose ack state as acked/ack_status/config_acked or a
    reported config_version (from HEARTBEAT/CONFIG_ACK) equal to the pushed one."""
    cfg = dev.get("config") if isinstance(dev.get("config"), dict) else {}
    for src in (dev, cfg):
        for k in ("acked", "config_acked", "ack"):
            if k in src and src[k] is not None:
                return bool(src[k])
        if "ack_status" in src and src["ack_status"] is not None:
            return str(src["ack_status"]).lower() in ("applied", "0", "ok", "acked")
    pushed = config_version_of(dev)
    reported = dev.get("reported_config_version") or cfg.get("reported_version") or cfg.get("device_version")
    if pushed is not None and reported is not None:
        return int(reported) >= pushed
    return None


# --------------------------------------------------------------------------- #
# Expectation evaluation — each returns (ok, detail)
# --------------------------------------------------------------------------- #
def eval_check(api: Api, e: dict) -> tuple[bool, str]:
    check, did, p = e["check"], e.get("device"), e.get("params", {}) or {}

    if check in ("alert_open", "alert_resolved"):
        alerts = [a for a in api.alerts(did) if alert_matches(a, p["match"])]
        if did:
            alerts = [a for a in alerts if alert_device(a) in (None, did)]
        want_open = check == "alert_open"
        hits = [a for a in alerts if alert_is_open(a) == want_open]
        if hits:
            a = hits[0]
            return True, f"{'open' if want_open else 'resolved'} alert #{a.get('id')} {a.get('type') or a.get('rule') or a.get('message', '')!s:.60}"
        if alerts:
            return False, f"{len(alerts)} matching alert(s) but none {'open' if want_open else 'resolved'}"
        return False, f"no alert matching {p['match']!r} for {did}"

    if check == "device_state":
        dev = api.device(did)
        if not dev:
            return False, f"{did} unknown to backend"
        problems = []
        if "online" in p and bool(dev.get("online")) != bool(p["online"]):
            problems.append(f"online={dev.get('online')}")
        lat = latest_of(dev)
        if p.get("has_telemetry") and not lat:
            problems.append("no latest telemetry")
        if "state" in p:
            want = [p["state"]] if isinstance(p["state"], str) else list(p["state"])
            got = norm_state(dev.get("state", lat.get("state")))
            if got not in want:
                problems.append(f"state={got} not in {want}")
        if "fault_flags_any" in p:
            flags = fault_names_from(lat.get("fault_flags", dev.get("fault_flags")))
            if not flags & set(p["fault_flags_any"]):
                problems.append(f"fault_flags={sorted(flags)} lacks any of {p['fault_flags_any']}")
        if "uptime_lt_s" in p:
            up = dev.get("uptime_s")
            if up is None and isinstance(dev.get("stats"), dict):
                up = dev["stats"].get("uptime_s")
            if up is None:
                problems.append("uptime_s not exposed")
            elif float(up) >= float(p["uptime_lt_s"]):
                problems.append(f"uptime_s={up} >= {p['uptime_lt_s']}")
        if problems:
            return False, "; ".join(problems)
        return True, f"online={dev.get('online')} state={norm_state(dev.get('state', lat.get('state')))}"

    if check == "device_fw":
        dev = api.device(did)
        if not dev:
            return False, f"{did} unknown to backend"
        got = fw_str(dev.get("fw_version") or dev.get("fw"))
        return got == p["version"], f"fw_version={got} (want {p['version']})"

    if check == "config_version":
        dev = api.device(did)
        if not dev:
            return False, f"{did} unknown to backend"
        v = config_version_of(dev)
        if v is None:
            return False, "config_version not exposed"
        if v < int(p.get("min", 1)):
            return False, f"config_version={v} < {p['min']}"
        if p.get("acked"):
            ack = config_acked(dev)
            if ack is False:
                return False, f"config_version={v} but not acked"
            if ack is None:
                return True, f"config_version={v} (ack state not exposed; accepted)"
        return True, f"config_version={v} acked={config_acked(dev)}"

    if check == "loss_pct_gt":
        dev = api.device(did)
        if not dev:
            return False, f"{did} unknown to backend"
        lp = loss_pct_of(dev)
        if lp is None:
            return False, "loss_pct not exposed"
        if p.get("invert"):
            return lp <= float(p["value"]), f"loss_pct={lp:.2f} (want <= {p['value']})"
        return lp > float(p["value"]), f"loss_pct={lp:.2f} (want > {p['value']})"

    if check == "diagnosis_nonempty":
        try:
            d = api.post(f"/api/devices/{did}/diagnose")
        except Exception as ex:
            return False, f"diagnose failed: {ex}"
        causes = (d or {}).get("probable_causes") or []
        return bool(causes), f"{len(causes)} probable_causes, model={(d or {}).get('model')}"

    if check == "no_alerts_of_severity":
        sev = p["severity"].lower()
        alerts = [a for a in api.alerts(did, status="open") if str(a.get("severity", "")).lower() == sev]
        if did:
            alerts = [a for a in alerts if alert_device(a) in (None, did)]
        if alerts:
            return False, f"{len(alerts)} open {sev} alert(s): " + ", ".join(
                str(a.get("type") or a.get("rule") or a.get("message", ""))[:40] for a in alerts[:3])
        return True, f"no open {sev} alerts"

    return False, f"unknown check {check}"


# --------------------------------------------------------------------------- #
# Timeline actions
# --------------------------------------------------------------------------- #
def run_action(api: Api, ev: dict, proxy_ctl: str | None) -> str:
    act, did, p = ev["action"], ev.get("device"), dict(ev.get("params", {}) or {})
    if act == "fault":
        body = {"set": p["set"], "clear": p.get("clear", []), "duration_ms": int(p.get("duration_ms", 0))}
        api.post(f"/api/devices/{did}/fault", body)
        return f"FAULT_INJECT set={p['set']} dur={body['duration_ms']}ms"
    if act == "clear_fault":
        api.post(f"/api/devices/{did}/fault", {"set": [], "clear": p["clear"], "duration_ms": 0})
        return f"FAULT_INJECT clear={p['clear']}"
    if act == "config":
        r = api.put(f"/api/devices/{did}/config", p)
        v = (r or {}).get("config_version") or (r or {}).get("version")
        return f"CONFIG_SET temp_limit_c={p.get('temp_limit_c')} -> version {v}"
    if act == "firmware_register":
        body = {"version": p["version"], "notes": p.get("notes", ""), "size_bytes": int(p.get("size_bytes", 1_000_000))}
        api.post("/api/firmware", body)
        return f"registered firmware {p['version']} ({body['size_bytes']} B)"
    if act == "firmware_deploy":
        r = api.post(f"/api/devices/{did}/firmware", {"version": p["version"]})
        return f"FW_UPDATE {p['version']} job={(r or {}).get('job_id') or (r or {}).get('id')}"
    if act == "cmd":
        api.post(f"/api/devices/{did}/cmd", {"cmd": p["cmd"]})
        return f"CMD {p['cmd']}"
    if act == "proxy_set":
        if not proxy_ctl:
            raise RuntimeError("proxy_set but no proxy control endpoint")
        import httpx
        r = httpx.post(f"{proxy_ctl}/set", json=p, timeout=5)
        r.raise_for_status()
        return f"proxy impairments -> {r.json().get('impairments')}"
    raise RuntimeError(f"unknown action {act}")


# --------------------------------------------------------------------------- #
# Process management
# --------------------------------------------------------------------------- #
@dataclass
class Child:
    name: str
    proc: subprocess.Popen
    log_path: Path | None = None


@dataclass
class Runner:
    sc: dict
    args: argparse.Namespace
    children: list[Child] = field(default_factory=list)
    t0: float = 0.0
    log_dir: Path = REPORT_DIR / "logs"

    # ----- spawning -----
    def spawn(self, name: str, cmd: list[str], cwd: Path | None = None, env: dict | None = None) -> Child:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{self.sc['name']}-{name}.log"
        fh = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd, cwd=str(cwd) if cwd else None, env=env, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        ch = Child(name, proc, log_path)
        self.children.append(ch)
        log(f"spawned {C.cyan(name)} pid={proc.pid}: {C.dim(' '.join(cmd))}", self.t0)
        return ch

    def kill_all(self):
        for ch in reversed(self.children):
            if ch.proc.poll() is None:
                try:
                    os.killpg(ch.proc.pid, signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    ch.proc.terminate()
        deadline = time.time() + 5
        for ch in self.children:
            while ch.proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if ch.proc.poll() is None:
                try:
                    os.killpg(ch.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    ch.proc.kill()
        self.children.clear()

    def check_children(self) -> list[str]:
        dead = []
        for ch in self.children:
            rc = ch.proc.poll()
            if rc is not None and ch.name.startswith("agent:"):
                dead.append(f"{ch.name} exited rc={rc}")
        return dead

    # ----- components -----
    def backend_python(self) -> str:
        venv = REPO_ROOT / "backend" / ".venv" / "bin" / "python"
        return str(venv) if venv.exists() else sys.executable

    def start_backend(self, api: Api):
        from urllib.parse import urlparse
        be = self.sc["backend"]
        http_port = urlparse(be["http"]).port or 8780
        env = dict(os.environ)
        env.update({
            "ECU_HTTP_PORT": str(http_port),
            "ECU_UDP_PORT": str(be["udp_port"]),
            "ECU_TCP_PORT": str(be["tcp_port"]),
            "ECU_DB_PATH": str(REPORT_DIR / f"{self.sc['name']}.db"),
            "PYTHONUNBUFFERED": "1",
        })
        for suffix in ("", "-wal", "-shm"):
            Path(env["ECU_DB_PATH"] + suffix).unlink(missing_ok=True)
        cmd = [self.backend_python(), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
               "--port", str(http_port), "--log-level", "warning"]
        self.spawn("backend", cmd, cwd=REPO_ROOT / "backend", env=env)
        deadline = time.time() + self.args.backend_timeout
        while time.time() < deadline:
            if api.health_ok():
                log(f"backend healthy at {be['http']}", self.t0)
                return
            if self.children[-1].proc.poll() is not None:
                raise RuntimeError(f"backend exited early; see {self.children[-1].log_path}")
            time.sleep(0.25)
        raise RuntimeError(f"backend did not become healthy within {self.args.backend_timeout}s")

    def start_proxy(self) -> str | None:
        px = self.sc.get("proxy")
        via = {d["id"] for d in self.sc["devices"] if d.get("via_proxy")} | set(self.args.via_proxy)
        if not via:
            return None
        if px is None:
            px = {"listen": self.args.proxy_port, "control_port": self.args.proxy_port + 9, "drop": 0.1}
            self.sc["proxy"] = px
        be = self.sc["backend"]
        ctl_port = px.get("control_port") or px["listen"] + 9
        px["control_port"] = ctl_port
        cmd = [sys.executable, str(SIM_DIR / "lossy_proxy.py"),
               "--listen", str(px["listen"]), "--bind", "127.0.0.1",
               "--forward", f"127.0.0.1:{be['udp_port']}", "--control-port", str(ctl_port),
               "--stats-every", "10"]
        for k in ("drop", "delay_ms", "jitter_ms", "reorder", "dup"):
            if k in px:
                cmd += [f"--{k.replace('_', '-')}", str(px[k])]
        self.spawn("lossy_proxy", cmd)
        ctl = f"http://127.0.0.1:{ctl_port}"
        import httpx
        for _ in range(40):
            try:
                if httpx.get(f"{ctl}/health", timeout=1).status_code == 200:
                    return ctl
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("lossy_proxy control endpoint did not come up")

    def agent_cmd(self, d: dict) -> list[str]:
        be = self.sc["backend"]
        via = d.get("via_proxy") or d["id"] in self.args.via_proxy
        udp_port = self.sc["proxy"]["listen"] if via else be["udp_port"]
        cmd = [str(self.args.agent), "--id", d["id"], "--host", "127.0.0.1",
               "--udp-port", str(udp_port), "--tcp-port", str(be["tcp_port"]),
               "--hz", str(d.get("hz", 10)), "--fw", str(d.get("fw", "1.0.0")),
               "--hw", str(d.get("hw", "SIM-MOTOR-A")), "--profile", d.get("profile", "city"),
               "--drop-rate", str(d.get("drop_rate", 0.0)), "--seed", str(d.get("seed", 1)),
               "--duration", str(int(self.sc["duration_s"]) + 10),
               "--log-level", d.get("log_level", "info")]
        for spec in d.get("faults", []) or []:
            cmd += ["--fault", spec]
        if d.get("no_gps"):
            cmd.append("--no-gps")
        return cmd

    # ----- main loop -----
    def run(self) -> dict:
        sc, args = self.sc, self.args
        api = Api(args.backend or sc["backend"]["http"])
        results: list[dict] = []
        actions: list[dict] = []
        fatal: str | None = None
        self.t0 = time.time()
        proxy_ctl = None
        try:
            if args.start_backend:
                self.start_backend(api)
            elif not api.health_ok():
                raise RuntimeError(f"backend not reachable at {api.base} (use --start-backend or start it yourself)")
            else:
                log(f"using running backend at {api.base}", self.t0)

            proxy_ctl = self.start_proxy()

            agent = Path(args.agent)
            if not agent.exists():
                raise RuntimeError(f"agent binary not found: {agent} (build firmware or pass --agent)")
            for d in sc["devices"]:
                self.spawn(f"agent:{d['id']}", self.agent_cmd(d))

            # Merge timeline + expectations into one schedule, ordered by time.
            sched: list[tuple[float, str, dict]] = [(float(ev["at_s"]), "action", ev) for ev in sc["timeline"]]
            sched += [(float(e["at_s"]), "expect", e) for e in sc["expect"]]
            sched.sort(key=lambda x: (x[0], 0 if x[1] == "action" else 1))
            end_at = max([float(sc["duration_s"])] + [t for t, _, _ in sched])

            # t0 for the scenario clock starts once agents are spawned
            clock0 = time.time()
            for at, kind, item in sched:
                wait = clock0 + at - time.time()
                if wait > 0:
                    time.sleep(wait)
                dead = self.check_children()
                for msg in dead:
                    log(C.red(f"WARNING: {msg}"), self.t0)
                if kind == "action":
                    try:
                        detail = run_action(api, item, proxy_ctl)
                        ok = True
                    except Exception as ex:
                        detail, ok = f"{type(ex).__name__}: {ex}", False
                    actions.append({"at_s": at, "action": item["action"], "device": item.get("device"),
                                    "ok": ok, "detail": detail})
                    log(f"{C.yellow('ACTION')} t={at:>5.1f} {item['action']:<18} {item.get('device') or '-':<10} "
                        f"{C.green(detail) if ok else C.red(detail)}", self.t0)
                else:
                    ok, detail = self.eval_with_grace(api, item)
                    results.append({"at_s": at, "check": item["check"], "device": item.get("device"),
                                    "params": item.get("params", {}), "ok": ok, "detail": detail})
                    tag = C.green("PASS") if ok else C.red("FAIL")
                    log(f"{tag}   t={at:>5.1f} {item['check']:<18} {item.get('device') or '-':<10} {detail}", self.t0)
                    if not ok and args.fail_fast:
                        break
            remaining = clock0 + end_at - time.time()
            if remaining > 0 and not args.fail_fast:
                time.sleep(min(remaining, 2.0))
        except KeyboardInterrupt:
            fatal = "interrupted"
            log(C.red("interrupted — shutting down children"), self.t0)
        except Exception as ex:
            fatal = f"{type(ex).__name__}: {ex}"
            log(C.red(f"FATAL: {fatal}"), self.t0)
        finally:
            self.kill_all()
            api.close()

        passed = sum(1 for r in results if r["ok"])
        report = {
            "scenario": sc["name"],
            "description": sc["description"].strip(),
            "started_at": datetime.fromtimestamp(self.t0, timezone.utc).isoformat(),
            "duration_s": round(time.time() - self.t0, 1),
            "backend": api.base,
            "agent": str(args.agent),
            "devices": [d["id"] for d in sc["devices"]],
            "actions": actions,
            "expectations": results,
            "passed": passed,
            "failed": len(results) - passed,
            "expected_total": len(sc["expect"]),
            "fatal": fatal,
            "ok": fatal is None and passed == len(sc["expect"]),
        }
        return report

    def eval_with_grace(self, api: Api, e: dict) -> tuple[bool, str]:
        deadline = time.time() + self.args.grace
        detail = ""
        while True:
            try:
                ok, detail = eval_check(api, e)
            except Exception as ex:
                ok, detail = False, f"{type(ex).__name__}: {ex}"
            if ok or time.time() >= deadline:
                return ok, detail
            time.sleep(POLL_S)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def print_table(report: dict):
    rows = report["expectations"]
    print()
    print(C.bold(f"SIL report — {report['scenario']}  ({report['duration_s']}s, {len(report['devices'])} devices)"))
    hdr = f"{'':6} {'t':>6}  {'check':<20} {'device':<10} detail"
    print(C.dim(hdr))
    print(C.dim("-" * 88))
    for r in rows:
        tag = C.green("PASS") if r["ok"] else C.red("FAIL")
        print(f"{tag:<6} {r['at_s']:>6.1f}  {r['check']:<20} {r['device'] or '-':<10} {r['detail']}")
    print(C.dim("-" * 88))
    summary = f"{report['passed']}/{report['expected_total']} expectations passed"
    if report["fatal"]:
        summary += f"  — FATAL: {report['fatal']}"
    print(C.green(C.bold(summary)) if report["ok"] else C.red(C.bold(summary)))


def write_report(report: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = REPORT_DIR / f"{report['scenario']}-{ts}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", nargs="+", help="scenario YAML file(s); several only allowed with --validate")
    p.add_argument("--validate", action="store_true", help="only validate the scenario schema and exit")
    p.add_argument("--backend", default=None, help="backend base URL (default: scenario backend.http)")
    p.add_argument("--start-backend", action="store_true",
                   help="start the backend (uvicorn from backend/, venv if present) on the scenario ports")
    p.add_argument("--backend-timeout", type=float, default=30.0, help="seconds to wait for /health")
    p.add_argument("--agent", default=str(DEFAULT_AGENT), help="path to ecu_agent binary")
    p.add_argument("--via-proxy", default="", help="comma-separated device ids to route through lossy_proxy")
    p.add_argument("--proxy-port", type=int, default=9781, help="proxy listen port when scenario has no proxy block")
    p.add_argument("--grace", type=float, default=GRACE_S, help="seconds to keep polling an expectation")
    p.add_argument("--fail-fast", action="store_true", help="stop at the first failed expectation")
    p.add_argument("--no-color", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_color:
        C.enabled = False
    args.via_proxy = [s.strip() for s in args.via_proxy.split(",") if s.strip()]

    if args.validate:
        rc = 0
        for path in args.scenario:
            try:
                with open(path) as f:
                    sc = yaml.safe_load(f)
                warnings = validate_scenario(sc)
                n_dev, n_tl, n_ex = len(sc["devices"]), len(sc["timeline"]), len(sc["expect"])
                print(f"{C.green('OK  ')} {path}: {sc['name']} — {n_dev} devices, {n_tl} actions, {n_ex} expectations, {sc['duration_s']}s")
                for w in warnings:
                    print(f"      {C.yellow('warn')} {w}")
            except (SchemaError, yaml.YAMLError, OSError) as ex:
                print(f"{C.red('FAIL')} {path}: {ex}")
                rc = 2
        return rc

    if len(args.scenario) != 1:
        print("exactly one scenario may be run at a time (use --validate for several)", file=sys.stderr)
        return 2
    try:
        sc = load_scenario(Path(args.scenario[0]))
    except (SchemaError, yaml.YAMLError, OSError) as ex:
        print(f"scenario error: {ex}", file=sys.stderr)
        return 2

    runner = Runner(sc, args)

    def _on_term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_term)
    log(C.bold(f"scenario {sc['name']}: {sc['description'].strip()}"))
    report = runner.run()
    print_table(report)
    out = write_report(report)
    print(C.dim(f"report: {out}"))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
