"""SQLite storage (stdlib sqlite3, WAL). Schema + queries + batched telemetry writer."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB = str(Path(__file__).resolve().parent.parent / "data" / "telemetry.db")
METRICS = ("rpm", "temp_c", "current_a", "voltage_v", "vibration_g", "speed_kph")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY,
  hw_model TEXT, fw_version TEXT, fw_version_u32 INTEGER,
  state INTEGER DEFAULT 0, online INTEGER DEFAULT 0,
  first_seen REAL, last_seen REAL, last_hello REAL, last_heartbeat REAL,
  telemetry_hz INTEGER, uptime_s INTEGER, device_reconnects INTEGER DEFAULT 0,
  reported_config_version INTEGER DEFAULT 0,
  lat REAL, lon REAL
);
CREATE TABLE IF NOT EXISTS telemetry (
  device_id TEXT NOT NULL, ts REAL NOT NULL, seq INTEGER,
  rpm REAL, temp_c REAL, current_a REAL, voltage_v REAL, vibration_g REAL, speed_kph REAL,
  lat REAL, lon REAL, fault_flags INTEGER, state INTEGER
);
CREATE INDEX IF NOT EXISTS ix_tel_dev_ts ON telemetry(device_id, ts);
CREATE TABLE IF NOT EXISTS telemetry_rollup_1m (
  device_id TEXT NOT NULL, bucket REAL NOT NULL, n INTEGER,
  rpm_avg REAL, rpm_min REAL, rpm_max REAL,
  temp_c_avg REAL, temp_c_min REAL, temp_c_max REAL,
  current_a_avg REAL, current_a_min REAL, current_a_max REAL,
  voltage_v_avg REAL, voltage_v_min REAL, voltage_v_max REAL,
  vibration_g_avg REAL, vibration_g_min REAL, vibration_g_max REAL,
  speed_kph_avg REAL, speed_kph_min REAL, speed_kph_max REAL,
  fault_flags_any INTEGER, state_max INTEGER, lat REAL, lon REAL,
  PRIMARY KEY (device_id, bucket)
);
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, ts REAL, device_ts_ms INTEGER,
  level INTEGER, message TEXT
);
CREATE INDEX IF NOT EXISTS ix_logs_dev ON logs(device_id, id);
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, rule TEXT, metric TEXT, value REAL,
  severity TEXT, status TEXT DEFAULT 'open', message TEXT,
  opened_at REAL, acked_at REAL, resolved_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS ix_alerts_dev ON alerts(device_id, status);
CREATE TABLE IF NOT EXISTS device_config (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, version INTEGER,
  telemetry_hz INTEGER, log_level INTEGER, rpm_limit REAL, temp_limit_c REAL, current_limit_a REAL,
  created_at REAL, ack_status TEXT DEFAULT 'pending', acked_at REAL,
  UNIQUE(device_id, version)
);
CREATE TABLE IF NOT EXISTS firmware (
  version TEXT PRIMARY KEY, notes TEXT, size_bytes INTEGER, crc INTEGER, created_at REAL
);
CREATE TABLE IF NOT EXISTS fw_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, target_version TEXT, from_version TEXT,
  status TEXT, created_at REAL, updated_at REAL, history TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_fwjobs_dev ON fw_jobs(device_id, id);
CREATE TABLE IF NOT EXISTS fault_injections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, set_flags INTEGER, clear_flags INTEGER,
  duration_ms INTEGER, created_at REAL, ack_flags INTEGER, acked_at REAL
);
CREATE TABLE IF NOT EXISTS rules (
  key TEXT PRIMARY KEY, value TEXT, updated_at REAL
);
CREATE TABLE IF NOT EXISTS device_stats (
  device_id TEXT PRIMARY KEY, frames_rx INTEGER DEFAULT 0, bad_crc INTEGER DEFAULT 0,
  seq_gaps INTEGER DEFAULT 0, loss_pct REAL DEFAULT 0, reconnects INTEGER DEFAULT 0,
  tcp_connected INTEGER DEFAULT 0, last_seen REAL, last_seq INTEGER, updated_at REAL
);
CREATE TABLE IF NOT EXISTS diagnoses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, created_at REAL, model TEXT,
  result TEXT, context TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


class Database:
    """Thread-safe wrapper: one connection guarded by a lock (writes are batched anyway)."""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("ECU_DB_PATH", DEFAULT_DB)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA temp_store=MEMORY")
            self.conn.executescript(SCHEMA)
        self._tel_buf: list[tuple] = []
        self._buf_lock = threading.Lock()
        self.retention_h = float(os.environ.get("ECU_RAW_RETENTION_H", "24"))

    def close(self):
        self.flush()
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------ helpers
    def q(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, tuple(params)).fetchall()]

    def q1(self, sql: str, params: Iterable = ()) -> dict | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params: Iterable = ()) -> int:
        """Execute; returns lastrowid for INSERTs, rowcount otherwise."""
        with self.lock:
            cur = self.conn.execute(sql, tuple(params))
            return cur.lastrowid if sql.lstrip()[:6].upper() == "INSERT" else cur.rowcount

    # ------------------------------------------------------------ devices
    def upsert_device(self, device_id: str, **fields):
        fields.setdefault("last_seen", time.time())
        cols = ", ".join(fields)
        sets = ", ".join(f"{k}=excluded.{k}" for k in fields)
        with self.lock:
            self.conn.execute(
                f"INSERT INTO devices (device_id, first_seen, {cols}) VALUES (?, ?, {','.join('?' * len(fields))}) "
                f"ON CONFLICT(device_id) DO UPDATE SET {sets}",
                (device_id, time.time(), *fields.values()),
            )

    def get_device(self, device_id: str) -> dict | None:
        return self.q1("SELECT * FROM devices WHERE device_id=?", (device_id,))

    def list_devices(self) -> list[dict]:
        return self.q("SELECT * FROM devices ORDER BY device_id")

    def set_online(self, device_id: str, online: bool):
        self.x("UPDATE devices SET online=? WHERE device_id=?", (1 if online else 0, device_id))

    # ------------------------------------------------------------ telemetry
    def buffer_telemetry(self, device_id: str, ts: float, seq: int, t: dict):
        row = (device_id, ts, seq, t["rpm"], t["temp_c"], t["current_a"], t["voltage_v"], t["vibration_g"],
               t["speed_kph"], t["lat"], t["lon"], t["fault_flags"], t["state"])
        with self._buf_lock:
            self._tel_buf.append(row)

    def flush(self) -> int:
        with self._buf_lock:
            rows, self._tel_buf = self._tel_buf, []
        if not rows:
            return 0
        with self.lock:
            self.conn.execute("BEGIN")
            self.conn.executemany("INSERT INTO telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.execute("COMMIT")
        return len(rows)

    def telemetry(self, device_id: str, since: float | None = None, until: float | None = None,
                  limit: int = 2000, step: str = "raw") -> list[dict]:
        now = time.time()
        since = since if since is not None else now - 300
        until = until if until is not None else now + 1
        limit = max(1, min(int(limit), 20000))
        if step in ("1m", "60s"):
            rows = self.q(
                "SELECT bucket AS ts, n, rpm_avg AS rpm, temp_c_avg AS temp_c, current_a_avg AS current_a, "
                "voltage_v_avg AS voltage_v, vibration_g_avg AS vibration_g, speed_kph_avg AS speed_kph, "
                "lat, lon, fault_flags_any AS fault_flags, state_max AS state, "
                "rpm_min, rpm_max, temp_c_min, temp_c_max, current_a_min, current_a_max, voltage_v_min, voltage_v_max, "
                "vibration_g_min, vibration_g_max, speed_kph_min, speed_kph_max "
                "FROM telemetry_rollup_1m WHERE device_id=? AND bucket>=? AND bucket<? ORDER BY bucket DESC LIMIT ?",
                (device_id, since - 60, until, limit))
            rows.reverse()
            # append any not-yet-rolled raw data aggregated on the fly
            last_bucket = rows[-1]["ts"] + 60 if rows else since
            rows += self._agg_raw(device_id, max(last_bucket, since), until, 60)
            return [self._fmt(r) for r in rows][-limit:]
        if step in ("10s",):
            return [self._fmt(r) for r in self._agg_raw(device_id, since, until, 10)][-limit:]
        rows = self.q("SELECT * FROM telemetry WHERE device_id=? AND ts>=? AND ts<? ORDER BY ts DESC LIMIT ?",
                      (device_id, since, until, limit))
        rows.reverse()
        return [self._fmt(r) for r in rows]

    def _agg_raw(self, device_id: str, since: float, until: float, width: int) -> list[dict]:
        sel = ", ".join(f"AVG({m}) AS {m}, MIN({m}) AS {m}_min, MAX({m}) AS {m}_max" for m in METRICS)
        return self.q(
            f"SELECT CAST(ts/{width} AS INTEGER)*{width} AS ts, COUNT(*) AS n, {sel}, "
            "AVG(lat) AS lat, AVG(lon) AS lon, MAX(fault_flags) AS fault_flags, MAX(state) AS state "
            f"FROM telemetry WHERE device_id=? AND ts>=? AND ts<? GROUP BY 1 ORDER BY 1",
            (device_id, since, until))

    @staticmethod
    def _fmt(r: dict) -> dict:
        r = dict(r)
        r["ts_iso"] = iso(r["ts"])
        return r

    def latest_telemetry(self, device_id: str) -> dict | None:
        r = self.q1("SELECT * FROM telemetry WHERE device_id=? ORDER BY ts DESC LIMIT 1", (device_id,))
        return self._fmt(r) if r else None

    def metric_stats(self, device_id: str, window_s: float = 300) -> dict:
        since = time.time() - window_s
        sel = ", ".join(f"AVG({m}) AS {m}_avg, MIN({m}) AS {m}_min, MAX({m}) AS {m}_max" for m in METRICS)
        r = self.q1(f"SELECT COUNT(*) AS n, {sel} FROM telemetry WHERE device_id=? AND ts>=?", (device_id, since))
        if not r or not r["n"]:
            return {"n": 0}
        out = {"n": r["n"]}
        for m in METRICS:
            out[m] = {"avg": round(r[f"{m}_avg"], 3), "min": round(r[f"{m}_min"], 3), "max": round(r[f"{m}_max"], 3)}
        return out

    # ------------------------------------------------------------ rollups / retention
    def rollup(self, upto: float | None = None) -> int:
        """Roll raw rows into 1-minute buckets for all complete minutes not yet rolled."""
        upto = upto or time.time()
        last = self.q1("SELECT value FROM meta WHERE key='rollup_upto'")
        start = float(last["value"]) if last else 0.0
        end = (int(upto) // 60) * 60  # only complete minutes
        if end <= start:
            return 0
        sel = ", ".join(f"AVG({m}), MIN({m}), MAX({m})" for m in METRICS)
        with self.lock:
            self.conn.execute("BEGIN")
            cur = self.conn.execute(
                f"INSERT OR REPLACE INTO telemetry_rollup_1m SELECT device_id, CAST(ts/60 AS INTEGER)*60, COUNT(*), {sel}, "
                "MAX(fault_flags), MAX(state), AVG(lat), AVG(lon) FROM telemetry WHERE ts>=? AND ts<? GROUP BY device_id, 2",
                (start, end))
            n = cur.rowcount
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('rollup_upto', ?)", (str(end),))
            self.conn.execute("COMMIT")
        return n

    def prune(self) -> int:
        cutoff = time.time() - self.retention_h * 3600
        n = self.x("DELETE FROM telemetry WHERE ts<?", (cutoff,))
        self.x("DELETE FROM logs WHERE ts<?", (cutoff - 6 * 86400,))
        return n

    # ------------------------------------------------------------ logs
    def add_log(self, device_id: str, level: int, message: str, device_ts_ms: int | None = None) -> dict:
        ts = time.time()
        lid = self.x("INSERT INTO logs (device_id, ts, device_ts_ms, level, message) VALUES (?,?,?,?,?)",
                     (device_id, ts, device_ts_ms, level, message))
        return {"id": lid, "device_id": device_id, "ts": ts, "ts_iso": iso(ts), "level": level, "message": message}

    def logs(self, device_id: str, limit: int = 200) -> list[dict]:
        rows = self.q("SELECT * FROM logs WHERE device_id=? ORDER BY id DESC LIMIT ?", (device_id, limit))
        for r in rows:
            r["ts_iso"] = iso(r["ts"])
        return rows

    # ------------------------------------------------------------ alerts
    def open_alert(self, device_id: str, rule: str) -> dict | None:
        return self.q1("SELECT * FROM alerts WHERE device_id=? AND rule=? AND status IN ('open','acked') ORDER BY id DESC LIMIT 1",
                       (device_id, rule))

    def create_alert(self, device_id: str, rule: str, metric: str | None, value: float | None, severity: str, message: str) -> dict:
        now = time.time()
        aid = self.x("INSERT INTO alerts (device_id, rule, metric, value, severity, status, message, opened_at, updated_at) "
                     "VALUES (?,?,?,?,?,'open',?,?,?)", (device_id, rule, metric, value, severity, message, now, now))
        return self.get_alert(aid)

    def update_alert(self, aid: int, **fields):
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE alerts SET {sets} WHERE id=?", (*fields.values(), aid))

    def get_alert(self, aid: int) -> dict | None:
        r = self.q1("SELECT * FROM alerts WHERE id=?", (aid,))
        return self._fmt_alert(r) if r else None

    @staticmethod
    def _fmt_alert(r: dict) -> dict:
        r = dict(r)
        for k in ("opened_at", "acked_at", "resolved_at", "updated_at"):
            r[k + "_iso"] = iso(r.get(k))
        return r

    def alerts(self, device_id: str | None = None, status: str = "open", limit: int = 200) -> list[dict]:
        where, params = [], []
        if device_id:
            where.append("device_id=?"); params.append(device_id)
        if status == "open":
            where.append("status IN ('open','acked')")
        elif status in ("resolved", "acked"):
            where.append("status=?"); params.append(status)
        sql = "SELECT * FROM alerts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [self._fmt_alert(r) for r in self.q(sql, params)]

    def open_alert_counts(self) -> dict[str, int]:
        return {r["device_id"]: r["n"] for r in
                self.q("SELECT device_id, COUNT(*) AS n FROM alerts WHERE status IN ('open','acked') GROUP BY device_id")}

    # ------------------------------------------------------------ config
    def current_config(self, device_id: str) -> dict | None:
        return self.q1("SELECT * FROM device_config WHERE device_id=? ORDER BY version DESC LIMIT 1", (device_id,))

    def config_history(self, device_id: str, limit: int = 20) -> list[dict]:
        return self.q("SELECT * FROM device_config WHERE device_id=? ORDER BY version DESC LIMIT ?", (device_id, limit))

    def new_config(self, device_id: str, telemetry_hz: int, log_level: int, rpm_limit: float,
                   temp_limit_c: float, current_limit_a: float) -> dict:
        cur = self.current_config(device_id)
        dev = self.get_device(device_id)
        base = max(cur["version"] if cur else 0, (dev or {}).get("reported_config_version") or 0)
        version = base + 1
        self.x("INSERT INTO device_config (device_id, version, telemetry_hz, log_level, rpm_limit, temp_limit_c, "
               "current_limit_a, created_at) VALUES (?,?,?,?,?,?,?,?)",
               (device_id, version, telemetry_hz, log_level, rpm_limit, temp_limit_c, current_limit_a, time.time()))
        return self.current_config(device_id)

    def ack_config(self, device_id: str, version: int, status: str):
        self.x("UPDATE device_config SET ack_status=?, acked_at=? WHERE device_id=? AND version=?",
               (status, time.time(), device_id, version))

    # ------------------------------------------------------------ firmware
    def firmware_list(self) -> list[dict]:
        rows = self.q("SELECT * FROM firmware ORDER BY created_at DESC")
        for r in rows:
            r["created_at_iso"] = iso(r["created_at"])
        return rows

    def firmware_get(self, version: str) -> dict | None:
        return self.q1("SELECT * FROM firmware WHERE version=?", (version,))

    def firmware_add(self, version: str, notes: str, size_bytes: int, crc: int) -> dict:
        self.x("INSERT OR REPLACE INTO firmware VALUES (?,?,?,?,?)", (version, notes, size_bytes, crc, time.time()))
        return self.firmware_get(version)

    def fw_job_create(self, device_id: str, target: str, from_version: str | None) -> dict:
        now = time.time()
        jid = self.x("INSERT INTO fw_jobs (device_id, target_version, from_version, status, created_at, updated_at, history) "
                     "VALUES (?,?,?,'pending',?,?,?)",
                     (device_id, target, from_version, now, now, json.dumps([{"status": "pending", "ts": iso(now)}])))
        return self.fw_job_get(jid)

    def fw_job_get(self, jid: int) -> dict | None:
        r = self.q1("SELECT * FROM fw_jobs WHERE id=?", (jid,))
        return self._fmt_job(r) if r else None

    def fw_job_latest(self, device_id: str) -> dict | None:
        r = self.q1("SELECT * FROM fw_jobs WHERE device_id=? ORDER BY id DESC LIMIT 1", (device_id,))
        return self._fmt_job(r) if r else None

    def fw_job_update(self, jid: int, status: str):
        job = self.q1("SELECT history FROM fw_jobs WHERE id=?", (jid,))
        if not job:
            return
        hist = json.loads(job["history"] or "[]")
        now = time.time()
        hist.append({"status": status, "ts": iso(now)})
        self.x("UPDATE fw_jobs SET status=?, updated_at=?, history=? WHERE id=?", (status, now, json.dumps(hist), jid))

    @staticmethod
    def _fmt_job(r: dict) -> dict:
        r = dict(r)
        r["history"] = json.loads(r.get("history") or "[]")
        r["created_at_iso"] = iso(r["created_at"])
        r["updated_at_iso"] = iso(r["updated_at"])
        return r

    # ------------------------------------------------------------ faults
    def fault_injection_add(self, device_id: str, set_flags: int, clear_flags: int, duration_ms: int) -> int:
        return self.x("INSERT INTO fault_injections (device_id, set_flags, clear_flags, duration_ms, created_at) VALUES (?,?,?,?,?)",
                      (device_id, set_flags, clear_flags, duration_ms, time.time()))

    def fault_injection_ack(self, device_id: str, active_flags: int):
        self.x("UPDATE fault_injections SET ack_flags=?, acked_at=? WHERE id=(SELECT id FROM fault_injections WHERE device_id=? "
               "ORDER BY id DESC LIMIT 1)", (active_flags, time.time(), device_id))

    def fault_injections(self, device_id: str, limit: int = 20) -> list[dict]:
        rows = self.q("SELECT * FROM fault_injections WHERE device_id=? ORDER BY id DESC LIMIT ?", (device_id, limit))
        for r in rows:
            r["created_at_iso"] = iso(r["created_at"])
        return rows

    # ------------------------------------------------------------ rules
    def get_rules(self) -> dict | None:
        r = self.q1("SELECT value FROM rules WHERE key='rules'")
        return json.loads(r["value"]) if r else None

    def set_rules(self, rules: dict):
        self.x("INSERT OR REPLACE INTO rules VALUES ('rules', ?, ?)", (json.dumps(rules), time.time()))

    # ------------------------------------------------------------ stats
    def save_stats(self, device_id: str, s: dict):
        self.x("INSERT INTO device_stats (device_id, frames_rx, bad_crc, seq_gaps, loss_pct, reconnects, tcp_connected, "
               "last_seen, last_seq, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
               "frames_rx=excluded.frames_rx, bad_crc=excluded.bad_crc, seq_gaps=excluded.seq_gaps, loss_pct=excluded.loss_pct, "
               "reconnects=excluded.reconnects, tcp_connected=excluded.tcp_connected, last_seen=excluded.last_seen, "
               "last_seq=excluded.last_seq, updated_at=excluded.updated_at",
               (device_id, s.get("frames_rx", 0), s.get("bad_crc", 0), s.get("seq_gaps", 0), s.get("loss_pct", 0.0),
                s.get("reconnects", 0), 1 if s.get("tcp_connected") else 0, s.get("last_seen"), s.get("last_seq"), time.time()))

    def load_stats(self) -> dict[str, dict]:
        return {r["device_id"]: r for r in self.q("SELECT * FROM device_stats")}

    # ------------------------------------------------------------ diagnoses
    def save_diagnosis(self, device_id: str, model: str, result: dict, context: dict) -> dict:
        now = time.time()
        did = self.x("INSERT INTO diagnoses (device_id, created_at, model, result, context) VALUES (?,?,?,?,?)",
                     (device_id, now, model, json.dumps(result), json.dumps(context)))
        return {"id": did, "device_id": device_id, "created_at": iso(now), "model": model, **result}

    def diagnoses(self, device_id: str, limit: int = 10) -> list[dict]:
        out = []
        for r in self.q("SELECT * FROM diagnoses WHERE device_id=? ORDER BY id DESC LIMIT ?", (device_id, limit)):
            out.append({"id": r["id"], "device_id": device_id, "created_at": iso(r["created_at"]), "model": r["model"],
                        **json.loads(r["result"])})
        return out


async def maintenance_loop(db: Database, flush_s: float = 0.5, rollup_s: float = 30.0, prune_s: float = 600.0):
    """Flush telemetry buffer every 0.5 s; roll up + prune periodically."""
    last_roll = last_prune = time.time()
    while True:
        await asyncio.sleep(flush_s)
        try:
            await asyncio.to_thread(db.flush)
            now = time.time()
            if now - last_roll > rollup_s:
                last_roll = now
                await asyncio.to_thread(db.rollup)
            if now - last_prune > prune_s:
                last_prune = now
                await asyncio.to_thread(db.prune)
        except Exception as e:  # pragma: no cover
            print("maintenance error:", e)
