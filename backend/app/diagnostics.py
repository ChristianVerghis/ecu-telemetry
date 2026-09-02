"""AI-assisted diagnostics with a deterministic heuristic fallback.

Uses the official Anthropic SDK (AsyncAnthropic, claude-opus-5, adaptive thinking, JSON-schema
structured output). If ANTHROPIC_API_KEY is unset or the call fails, the heuristic diagnostician runs
and the result is tagged model:"heuristic".
"""
from __future__ import annotations

import json
import os
import time

from .db import Database, iso
from .protocol import flags_to_names

MODEL = os.environ.get("ECU_DIAG_MODEL", "claude-opus-5")

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "probable_causes": {
            "type": "array",
            "items": {"type": "object",
                      "properties": {"cause": {"type": "string"}, "evidence": {"type": "string"},
                                     "likelihood": {"type": "number"}},
                      "required": ["cause", "evidence", "likelihood"], "additionalProperties": False},
        },
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["summary", "probable_causes", "recommended_actions", "confidence"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a vehicle ECU diagnostics engineer analysing telemetry from a 48 V electric traction motor controller. "
    "You are given a compact JSON context: device metadata, active config, firmware, 5-minute per-metric stats, "
    "open and recent alerts, recent device logs, recent SIL fault injections and packet-loss figures. "
    "Return a diagnosis as JSON matching the provided schema. Be concrete: cite the metrics/alerts/logs that support "
    "each cause, distinguish injected test faults from organic ones, and keep likelihoods in [0,1] summing to about 1. "
    "confidence in [0,1] reflects how well the evidence supports the summary."
)


class Diagnostician:
    def __init__(self, db: Database, ingest=None):
        self.db = db
        self.ingest = ingest
        self._client = None

    # ------------------------------------------------------------ context
    def build_context(self, device_id: str) -> dict:
        dev = self.db.get_device(device_id) or {"device_id": device_id}
        stats = self.ingest.st(device_id).as_dict() if self.ingest else {}
        cfg = self.db.current_config(device_id)
        latest = (self.ingest.latest.get(device_id) if self.ingest else None) or self.db.latest_telemetry(device_id)
        open_alerts = self.db.alerts(device_id, "open", 50)
        recent = [a for a in self.db.alerts(device_id, "all", 30) if a["status"] == "resolved"][:15]
        logs = self.db.logs(device_id, 30)
        job = self.db.fw_job_latest(device_id)
        return {
            "device": {"device_id": device_id, "hw_model": dev.get("hw_model"), "fw_version": dev.get("fw_version"),
                       "state": dev.get("state"), "online": bool(dev.get("online")), "uptime_s": dev.get("uptime_s"),
                       "device_reconnects": dev.get("device_reconnects"), "last_seen": iso(dev.get("last_seen"))},
            "config": {k: cfg[k] for k in ("version", "telemetry_hz", "log_level", "rpm_limit", "temp_limit_c",
                                            "current_limit_a", "ack_status")} if cfg else None,
            "fw_job": {"target": job["target_version"], "status": job["status"]} if job else None,
            "latest": {k: latest.get(k) for k in ("rpm", "temp_c", "current_a", "voltage_v", "vibration_g", "speed_kph",
                                                  "fault_flags", "state")} if latest else None,
            "active_faults": flags_to_names(int(latest.get("fault_flags") or 0)) if latest else [],
            "stats_5m": self.db.metric_stats(device_id, 300),
            "comm": {"loss_pct": stats.get("loss_pct"), "seq_gaps": stats.get("seq_gaps"), "bad_crc": stats.get("bad_crc"),
                     "reconnects": stats.get("reconnects"), "tcp_connected": stats.get("tcp_connected")},
            "open_alerts": [{"rule": a["rule"], "severity": a["severity"], "metric": a["metric"], "value": a["value"],
                             "since": a["opened_at_iso"], "message": a["message"]} for a in open_alerts],
            "recent_resolved_alerts": [{"rule": a["rule"], "severity": a["severity"], "opened": a["opened_at_iso"],
                                        "resolved": a["resolved_at_iso"]} for a in recent],
            "logs": [{"ts": l["ts_iso"], "level": l["level"], "msg": l["message"][:200]} for l in logs],
            "fault_injections": [{"at": f["created_at_iso"], "set": flags_to_names(f["set_flags"] or 0),
                                  "clear": flags_to_names(f["clear_flags"] or 0), "duration_ms": f["duration_ms"]}
                                 for f in self.db.fault_injections(device_id, 10)],
        }

    # ------------------------------------------------------------ entry point
    async def diagnose(self, device_id: str) -> dict:
        ctx = self.build_context(device_id)
        result, model = None, "heuristic"
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                result = await self._ask_claude(ctx)
                model = MODEL
            except Exception as e:  # any SDK/network/schema failure -> heuristic
                print(f"diagnostics: Claude call failed ({e!r}); using heuristic")
                result = None
        if result is None:
            result = heuristic_diagnosis(ctx)
            model = "heuristic"
        result["model"] = model
        return self.db.save_diagnosis(device_id, model, result, ctx)

    async def _ask_claude(self, ctx: dict) -> dict:
        import anthropic

        if self._client is None:
            self._client = anthropic.AsyncAnthropic()
        resp = await self._client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": "Diagnose this device.\n\n" + json.dumps(ctx, indent=None)}],
            # server-side refusal fallback (beta) - route by category, no model list to maintain
            extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
            extra_body={"fallbacks": "default"},
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("refused")
        text = next(b.text for b in resp.content if b.type == "text")
        data = json.loads(text)
        for k in ("summary", "probable_causes", "recommended_actions", "confidence"):
            if k not in data:
                raise ValueError(f"missing {k}")
        return data


# ---------------------------------------------------------------- heuristic

def heuristic_diagnosis(ctx: dict) -> dict:
    rules = {a["rule"] for a in ctx.get("open_alerts", [])}
    faults = set(ctx.get("active_faults") or [])
    for a in ctx.get("open_alerts", []):
        if a["rule"].startswith("flag:"):
            faults.add(a["rule"][5:])
    latest = ctx.get("latest") or {}
    stats = ctx.get("stats_5m") or {}
    comm = ctx.get("comm") or {}
    cfg = ctx.get("config") or {}
    injected = set()
    for f in ctx.get("fault_injections", []):
        injected.update(f.get("set", []))
    causes: list[dict] = []
    actions: list[str] = []

    def add(cause, evidence, lik):
        causes.append({"cause": cause, "evidence": evidence, "likelihood": lik})

    temp_rules = {"temp_high", "temp_critical", "rate:temp_c", "anomaly:temp_c"} & rules
    if temp_rules or "OVERTEMP" in faults:
        ev = f"alerts {sorted(temp_rules)}; temp_c stats {stats.get('temp_c')}; limit {cfg.get('temp_limit_c')}"
        if "OVERTEMP" in injected:
            add("SIL-injected OVERTEMP fault (test scenario)", ev + "; OVERTEMP present in recent fault injections", 0.6)
        if "current_high" in rules or "OVERCURRENT" in faults:
            add("Thermal runaway from sustained overcurrent (winding I^2R heating)",
                ev + f"; current_a stats {stats.get('current_a')}", 0.5)
        elif "rate:temp_c" in rules:
            add("Rapid heating: cooling loss or stalled rotor / high load at low rpm",
                ev + f"; rpm stats {stats.get('rpm')}", 0.45)
        else:
            add("Winding overtemperature due to cooling degradation or ambient load", ev, 0.4)
        actions += ["Derate torque/current until temp_c falls below limit", "Inspect coolant flow / fan and heatsink contact",
                    "Lower current_limit_a via config if load profile allows"]
    if "current_high" in rules or "OVERCURRENT" in faults:
        add("Phase overcurrent: mechanical overload, short, or aggressive torque demand",
            f"current_a stats {stats.get('current_a')}; limit {cfg.get('current_limit_a')}", 0.45)
        actions += ["Check for mechanical binding / brake drag", "Inspect phase wiring and insulation resistance"]
    if "voltage_low" in rules or "UNDERVOLTAGE" in faults or "anomaly:voltage_v" in rules:
        add("DC bus undervoltage: weak battery / high-resistance connection / regen fault",
            f"voltage_v stats {stats.get('voltage_v')} (48 V system)", 0.45)
        actions += ["Check battery SoC and pack balance", "Measure bus voltage drop under load at the connector"]
    if "vibration_high" in rules or "VIBRATION_HIGH" in faults or "anomaly:vibration_g" in rules:
        add("Mechanical imbalance / bearing wear / loose mount",
            f"vibration_g stats {stats.get('vibration_g')}", 0.45)
        actions += ["Inspect bearings and motor mounts", "Perform a coast-down vibration spectrum check"]
    if "ENCODER_FAULT" in faults or "anomaly:rpm" in rules or "rpm_high" in rules:
        add("Encoder/position feedback fault causing rpm instability",
            f"rpm stats {stats.get('rpm')}; faults {sorted(faults)}", 0.4)
        actions += ["Check encoder cabling and shielding", "Re-run encoder alignment/calibration"]
    if "SENSOR_STUCK" in faults:
        add("Stuck sensor reading (frozen ADC channel or broken sensor)", "SENSOR_STUCK flag reported by device", 0.5)
        actions += ["Compare stuck channel to redundant/estimated value", "Replace sensor or harness"]
    if "GPS_LOST" in faults:
        add("GPS fix lost (antenna, tunnel/urban canyon, or receiver fault)", "GPS_LOST flag", 0.35)
        actions += ["Verify GPS antenna connection; ignore position-derived metrics until fix returns"]
    if "packet_loss" in rules or "COMM_DEGRADED" in faults or (comm.get("loss_pct") or 0) > 10:
        add("Degraded UDP link (RF interference, congestion, or lossy path)",
            f"loss_pct {comm.get('loss_pct')}, seq_gaps {comm.get('seq_gaps')}, bad_crc {comm.get('bad_crc')}", 0.5)
        actions += ["Inspect network path / antenna; reduce telemetry_hz if link budget is marginal"]
    if "offline" in rules:
        add("Device offline: power loss, reboot loop, or network partition",
            f"no HEARTBEAT/TELEMETRY; reconnects {comm.get('reconnects')}; last_seen {ctx['device'].get('last_seen')}", 0.6)
        actions += ["Check device power and network reachability", "Review last logs before disconnect"]
    if (comm.get("reconnects") or 0) > 3 and "offline" not in rules:
        add("Unstable TCP control link (frequent reconnects)", f"reconnects {comm.get('reconnects')}", 0.3)
    if (ctx.get("fw_job") or {}).get("status") in ("rejected", "failed"):
        add("Firmware update rejected by device (version downgrade or malformed image)",
            f"fw_job {ctx.get('fw_job')}", 0.5)
        actions += ["Choose a firmware version higher than the installed one"]
    if cfg and cfg.get("ack_status") in ("rejected", "queued", "pending", "sent"):
        add("Config push not acknowledged/applied", f"config v{cfg.get('version')} ack_status={cfg.get('ack_status')}", 0.25)
        actions += ["Re-send config once the device control channel reconnects"]

    if not causes:
        summary = "No active faults or alerts; telemetry within configured limits. Device appears healthy."
        add("Nominal operation", f"no open alerts, faults {sorted(faults) or 'none'}, loss_pct {comm.get('loss_pct')}", 0.9)
        actions = ["No action required; continue monitoring"]
        conf = 0.8 if (stats.get("n") or 0) > 0 else 0.4
    else:
        total = sum(c["likelihood"] for c in causes) or 1.0
        for c in causes:
            c["likelihood"] = round(c["likelihood"] / total, 2)
        causes.sort(key=lambda c: -c["likelihood"])
        top = causes[0]["cause"]
        summary = (f"{len(rules)} open alert(s) {sorted(rules)}; active faults {sorted(faults) or 'none'}. "
                   f"Most likely: {top}.")
        conf = min(0.9, 0.4 + 0.1 * len(rules) + (0.2 if faults & injected else 0.0))
    # de-dupe actions preserving order
    seen, dedup = set(), []
    for a in actions:
        if a not in seen:
            seen.add(a); dedup.append(a)
    return {"summary": summary, "probable_causes": causes, "recommended_actions": dedup, "confidence": round(conf, 2),
            "generated_at": iso(time.time())}
