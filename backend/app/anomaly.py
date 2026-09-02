"""Rule engine: thresholds, fault flags, comm/offline, rate-of-change, EWMA z-score anomalies.

Dedupe: one open alert per (device, rule). Auto-resolve after the condition has been clear
for `resolve_after_s` (default 10 s). Rules are JSON, editable via /api/rules.
"""
from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field

from .protocol import FAULT_FLAGS, flags_to_names

DEFAULT_RULES: dict = {
    "resolve_after_s": 10,
    "thresholds": [
        {"name": "temp_high", "metric": "temp_c", "op": ">", "value": "temp_limit_c", "severity": "warn"},
        {"name": "temp_critical", "metric": "temp_c", "op": ">", "value": "temp_limit_c+15", "severity": "critical"},
        {"name": "current_high", "metric": "current_a", "op": ">", "value": "current_limit_a", "severity": "warn"},
        {"name": "rpm_high", "metric": "rpm", "op": ">", "value": "rpm_limit", "severity": "warn"},
        {"name": "voltage_low", "metric": "voltage_v", "op": "<", "value": 80.0, "severity": "warn"},
        {"name": "vibration_high", "metric": "vibration_g", "op": ">", "value": 2.5, "severity": "warn"},
    ],
    "comm": {"loss_pct": 10.0, "severity": "warn"},
    "offline": {"enabled": True, "severity": "critical"},
    "fault_flags": {"enabled": True, "severity": {"OVERTEMP": "critical", "OVERCURRENT": "critical",
                                                  "UNDERVOLTAGE": "warn", "VIBRATION_HIGH": "warn",
                                                  "SENSOR_STUCK": "warn", "GPS_LOST": "info",
                                                  "COMM_DEGRADED": "warn", "ENCODER_FAULT": "critical"}},
    "rate": {"temp_c": {"max_per_s": 2.0, "window_s": 5.0, "severity": "warn"}},
    "anomaly": {"enabled": True, "z": 4.0, "sustain": 3, "alpha": 0.05, "warmup": 20, "min_std": 0.05,
                "metrics": ["temp_c", "current_a", "voltage_v", "vibration_g"],  # rpm/speed are driver-controlled
                "severity": "warn"},
}

DEFAULT_CONFIG = {"telemetry_hz": 10, "log_level": 1, "rpm_limit": 6000.0, "temp_limit_c": 90.0, "current_limit_a": 120.0}


@dataclass
class Ewma:
    mean: float = 0.0
    var: float = 0.0
    n: int = 0
    streak: int = 0

    def update(self, x: float, alpha: float) -> float:
        """Return z-score of x against the state *before* update, then update."""
        if self.n == 0:
            self.mean, self.var, self.n = x, 0.0, 1
            return 0.0
        std = math.sqrt(self.var)
        z = abs(x - self.mean) / std if std > 0 else 0.0
        d = x - self.mean
        self.mean += alpha * d
        self.var = (1 - alpha) * (self.var + alpha * d * d)
        self.n += 1
        return z


@dataclass
class DeviceState:
    rate_hist: dict = field(default_factory=dict)
    ewma: dict[str, Ewma] = field(default_factory=dict)
    # rule -> last time condition was observed true
    active: dict[str, float] = field(default_factory=dict)
    # rule -> last value/message (for alert creation)
    pending_meta: dict[str, tuple] = field(default_factory=dict)


class AnomalyEngine:
    def __init__(self, db, bus=None, rules: dict | None = None):
        self.db = db
        self.bus = bus
        stored = db.get_rules() if db else None
        self.rules = self._merge(rules or stored or {})
        self.devices: dict[str, DeviceState] = {}
        self.config_cache: dict[str, dict] = {}

    # ------------------------------------------------------------ rules
    @staticmethod
    def _merge(overrides: dict) -> dict:
        r = copy.deepcopy(DEFAULT_RULES)
        for k, v in overrides.items():
            if isinstance(v, dict) and isinstance(r.get(k), dict) and k not in ("rate",):
                r[k].update(v)
            else:
                r[k] = v
        return r

    def set_rules(self, rules: dict) -> dict:
        self.rules = self._merge(rules)
        if self.db:
            self.db.set_rules(self.rules)
        return self.rules

    def config_for(self, device_id: str) -> dict:
        cfg = self.config_cache.get(device_id)
        if cfg is None:
            row = self.db.current_config(device_id) if self.db else None
            cfg = {k: row[k] for k in DEFAULT_CONFIG} if row else dict(DEFAULT_CONFIG)
            self.config_cache[device_id] = cfg
        return cfg

    def invalidate_config(self, device_id: str):
        self.config_cache.pop(device_id, None)

    def _resolve_value(self, spec, cfg: dict) -> float | None:
        if isinstance(spec, (int, float)):
            return float(spec)
        s = str(spec).replace(" ", "")
        key, off = s, 0.0
        for sep in ("+", "-"):
            if sep in s[1:]:
                key, num = s.rsplit(sep, 1)
                try:
                    off = float(num) * (1 if sep == "+" else -1)
                except ValueError:
                    return None
                break
        base = cfg.get(key)
        if base is None:
            return None
        return float(base) + off

    # ------------------------------------------------------------ alert lifecycle
    def _st(self, device_id: str) -> DeviceState:
        st = self.devices.get(device_id)
        if st is None:
            st = self.devices[device_id] = DeviceState()
        return st

    def _mark(self, device_id: str, rule: str, cond: bool, now: float, metric=None, value=None, severity="warn", message=""):
        st = self._st(device_id)
        if cond:
            st.active[rule] = now
            if not self.db.open_alert(device_id, rule):
                alert = self.db.create_alert(device_id, rule, metric, value, severity, message)
                self._emit(alert, "opened")

    def _emit(self, alert: dict, action: str):
        if self.bus and alert:
            self.bus.publish("alert", {"action": action, **alert})

    def resolve(self, device_id: str, rule: str, now: float | None = None):
        alert = self.db.open_alert(device_id, rule)
        st = self._st(device_id)
        st.active.pop(rule, None)
        if alert:
            self.db.update_alert(alert["id"], status="resolved", resolved_at=now or time.time())
            self._emit(self.db.get_alert(alert["id"]), "resolved")

    def tick(self, now: float | None = None):
        """Resolve alerts whose condition has been clear for resolve_after_s."""
        now = now or time.time()
        hold = float(self.rules.get("resolve_after_s", 10))
        for device_id, st in self.devices.items():
            for rule, last_true in list(st.active.items()):
                if rule == "offline":
                    continue
                if now - last_true >= hold:
                    self.resolve(device_id, rule, now)
        # also sweep DB-open alerts with no in-memory state (e.g. after restart) once they're stale
        for a in self.db.alerts(status="open", limit=1000):
            st = self.devices.get(a["device_id"])
            if a["rule"] == "offline":
                continue
            if st is None or a["rule"] not in st.active:
                if now - (a["updated_at"] or a["opened_at"]) >= hold:
                    self.resolve(a["device_id"], a["rule"], now)

    # ------------------------------------------------------------ inputs
    def on_offline(self, device_id: str, now: float | None = None):
        now = now or time.time()
        r = self.rules.get("offline", {})
        if r.get("enabled", True):
            self._mark(device_id, "offline", True, now, None, None, r.get("severity", "critical"),
                       "no HEARTBEAT/TELEMETRY for 15 s")

    def on_online(self, device_id: str, now: float | None = None):
        if self.db.open_alert(device_id, "offline"):
            self.resolve(device_id, "offline", now)

    def on_comm(self, device_id: str, loss_pct: float, now: float | None = None):
        now = now or time.time()
        r = self.rules.get("comm", {})
        thr = r.get("loss_pct")
        if thr is not None:
            self._mark(device_id, "packet_loss", loss_pct > float(thr), now, "loss_pct", loss_pct,
                       r.get("severity", "warn"), f"packet loss {loss_pct:.1f}% > {thr}%")

    def on_telemetry(self, device_id: str, t: dict, ts: float | None = None, loss_pct: float | None = None) -> list[str]:
        """Evaluate all rules for one telemetry sample. Returns rules currently true."""
        now = ts or time.time()
        st = self._st(device_id)
        cfg = self.config_for(device_id)
        fired: list[str] = []

        # thresholds
        for rule in self.rules.get("thresholds", []):
            metric = rule.get("metric")
            if metric not in t:
                continue
            thr = self._resolve_value(rule.get("value"), cfg)
            if thr is None:
                continue
            x = float(t[metric])
            op = rule.get("op", ">")
            cond = (x > thr) if op == ">" else (x < thr) if op == "<" else (x >= thr) if op == ">=" else (x <= thr)
            name = rule.get("name") or f"{metric}_{op}_{thr}"
            self._mark(device_id, name, cond, now, metric, x, rule.get("severity", "warn"),
                       f"{metric}={x:.2f} {op} {thr:.2f}")
            if cond:
                fired.append(name)

        # fault flags
        ff = self.rules.get("fault_flags", {})
        if ff.get("enabled", True):
            flags = int(t.get("fault_flags", 0))
            sev = ff.get("severity", {})
            for name in flags_to_names(flags):
                rule = f"flag:{name}"
                self._mark(device_id, rule, True, now, "fault_flags", float(FAULT_FLAGS[name]),
                           sev.get(name, "warn") if isinstance(sev, dict) else str(sev), f"device reports {name}")
                fired.append(rule)

        # rate of change — measured over a window (default 5 s) so per-sample sensor
        # noise at 10 Hz is not divided by a 0.1 s dt and reported as a runaway.
        for metric, spec in (self.rules.get("rate") or {}).items():
            if metric not in t:
                continue
            x = float(t[metric])
            hist = st.rate_hist.setdefault(metric, [])
            hist.append((now, x))
            window = float(spec.get("window_s", 5.0))
            while len(hist) > 1 and now - hist[0][0] > window * 2:
                hist.pop(0)
            # oldest sample at least `window` seconds back
            ref = next(((ts, v) for ts, v in hist if now - ts >= window), None)
            if ref is not None:
                dt = now - ref[0]
                rate = (x - ref[1]) / dt
                lim = float(spec.get("max_per_s", 2.0))
                cond = rate > lim
                self._mark(device_id, f"rate:{metric}", cond, now, metric, rate, spec.get("severity", "warn"),
                           f"{metric} rising {rate:.2f}/s over {dt:.0f}s > {lim}/s")
                if cond:
                    fired.append(f"rate:{metric}")

        # EWMA z-score anomaly
        an = self.rules.get("anomaly", {})
        if an.get("enabled", True):
            alpha = float(an.get("alpha", 0.05))
            zlim = float(an.get("z", 4.0))
            sustain = int(an.get("sustain", 3))
            warmup = int(an.get("warmup", 20))
            min_std = float(an.get("min_std", 0.05))
            for metric in an.get("metrics", []):
                if metric not in t:
                    continue
                x = float(t[metric])
                e = st.ewma.setdefault(metric, Ewma())
                warm = e.n >= warmup
                std = max(math.sqrt(e.var), min_std) if warm else math.sqrt(e.var)
                z = abs(x - e.mean) / std if (warm and std > 0) else 0.0
                # damp the baseline update on anomalous samples so outliers don't inflate the variance
                # and mask themselves; the baseline still re-learns a persistent level shift slowly
                e.update(x, alpha * 0.1 if (warm and z > zlim) else alpha)
                if warm and z > zlim:
                    e.streak += 1
                else:
                    e.streak = 0
                cond = e.streak >= sustain
                self._mark(device_id, f"anomaly:{metric}", cond, now, metric, x, an.get("severity", "warn"),
                           f"{metric}={x:.2f} deviates z={z:.1f} from EWMA {e.mean:.2f} for {e.streak} samples")
                if cond:
                    fired.append(f"anomaly:{metric}")

        if loss_pct is not None:
            self.on_comm(device_id, loss_pct, now)
        return fired
