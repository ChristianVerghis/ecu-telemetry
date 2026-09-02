import random

from app.anomaly import AnomalyEngine
from app.bus import EventBus

BASE = {"rpm": 3000.0, "temp_c": 60.0, "current_a": 50.0, "voltage_v": 96.0, "vibration_g": 0.5,
        "speed_kph": 40.0, "fault_flags": 0, "state": 1}


def test_threshold_fires_dedupes_resolves(db):
    bus = EventBus()
    q = bus.subscribe()
    eng = AnomalyEngine(db, bus)
    t0 = 1000.0
    eng.on_telemetry("D1", dict(BASE, temp_c=95.0), t0)
    eng.on_telemetry("D1", dict(BASE, temp_c=96.0), t0 + 1.0)
    eng.on_telemetry("D1", dict(BASE, temp_c=97.0), t0 + 2.0)
    opened = db.alerts("D1", "open")
    assert [a["rule"] for a in opened] == ["temp_high"]  # deduped: only one open alert
    assert opened[0]["severity"] == "warn"
    # critical threshold at limit + 15
    eng.on_telemetry("D1", dict(BASE, temp_c=106.0), t0 + 8.0)  # +9 C over 6 s stays under the 2 C/s rate rule
    rules = {a["rule"] for a in db.alerts("D1", "open")}
    assert rules == {"temp_high", "temp_critical"}
    # condition clears; not resolved until 10 s later
    eng.on_telemetry("D1", dict(BASE, temp_c=60.0), t0 + 9.0)
    eng.tick(t0 + 13.0)
    assert len(db.alerts("D1", "open")) == 2
    eng.tick(t0 + 20.0)
    assert db.alerts("D1", "open") == []
    assert {a["status"] for a in db.alerts("D1", "all")} == {"resolved"}
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    actions = [d["action"] for e, d in events if e == "alert"]
    assert actions.count("opened") == 2 and actions.count("resolved") == 2


def test_fault_flags_offline_comm_and_rate(db):
    eng = AnomalyEngine(db)
    eng.on_telemetry("D2", dict(BASE, fault_flags=0x21), 10.0, loss_pct=25.0)
    rules = {a["rule"]: a for a in db.alerts("D2", "open")}
    assert {"flag:OVERTEMP", "flag:GPS_LOST", "packet_loss"} <= set(rules)
    assert rules["flag:OVERTEMP"]["severity"] == "critical"
    # temp rate of change > 2 C/s, measured over the 5 s window (per-sample jitter must not trip it)
    eng.on_telemetry("D2", dict(BASE, temp_c=60.0), 20.0)
    eng.on_telemetry("D2", dict(BASE, temp_c=60.6), 20.1)  # 6 C/s instantaneous noise — ignored
    assert db.open_alert("D2", "rate:temp_c") is None
    eng.on_telemetry("D2", dict(BASE, temp_c=75.0), 25.5)  # +15 C over 5.5 s
    assert db.open_alert("D2", "rate:temp_c") is not None
    eng.on_offline("D2", 30.0)
    off = db.open_alert("D2", "offline")
    assert off and off["severity"] == "critical"
    eng.tick(100.0)  # offline never auto-resolves by timeout
    assert db.open_alert("D2", "offline") is not None
    eng.on_online("D2", 101.0)
    assert db.open_alert("D2", "offline") is None


def test_ewma_zscore_fires_on_step_change(db):
    eng = AnomalyEngine(db)
    eng.set_rules({"thresholds": [], "rate": {}})  # isolate the statistical rule
    rnd = random.Random(1)
    t = 0.0
    for _ in range(100):
        eng.on_telemetry("D3", dict(BASE, temp_c=60.0 + rnd.gauss(0, 0.3)), t)
        t += 0.1
    assert db.alerts("D3", "open") == []
    fired = []
    for _ in range(3):
        fired = eng.on_telemetry("D3", dict(BASE, temp_c=75.0), t)
        t += 0.1
    assert "anomaly:temp_c" in fired
    a = db.open_alert("D3", "anomaly:temp_c")
    assert a and a["metric"] == "temp_c"
    # other metrics stayed stable -> no anomaly alerts for them
    assert {x["rule"] for x in db.alerts("D3", "open")} == {"anomaly:temp_c"}


def test_rules_roundtrip_and_config_thresholds(db):
    eng = AnomalyEngine(db)
    db.new_config("D4", 10, 1, 6000, 70.0, 120.0)  # lower temp limit to 70
    eng.on_telemetry("D4", dict(BASE, temp_c=75.0), 1.0)
    assert db.open_alert("D4", "temp_high") is not None
    new = eng.set_rules({"thresholds": [{"name": "spd", "metric": "speed_kph", "op": ">", "value": 30, "severity": "info"}]})
    assert new["thresholds"][0]["name"] == "spd" and db.get_rules()["thresholds"][0]["name"] == "spd"
    eng2 = AnomalyEngine(db)  # reloads persisted rules
    assert eng2.rules["thresholds"][0]["name"] == "spd"
    eng2.on_telemetry("D4", dict(BASE), 2.0)
    assert db.open_alert("D4", "spd")["severity"] == "info"
