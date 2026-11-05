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


def _warm(eng, dev, hz=10, seconds=10.0, seed=1, **fixed):
    """Feed a steady, slightly noisy signal; returns the time after the last sample."""
    rnd, t, dt = random.Random(seed), 0.0, 1.0 / hz
    for _ in range(int(seconds * hz)):
        eng.on_telemetry(dev, dict(BASE, temp_c=60.0 + rnd.gauss(0, 0.3), current_a=50.0 + rnd.gauss(0, 0.5),
                                   vibration_g=0.5 + abs(rnd.gauss(0, 0.02)), **fixed), t)
        t += dt
    return t


def _feed(eng, dev, t, seconds, hz=10, **sample):
    fired, dt = [], 1.0 / hz
    for _ in range(int(round(seconds * hz))):
        fired = eng.on_telemetry(dev, dict(BASE, **sample), t)
        t += dt
    return fired, t


def test_ewma_zscore_fires_on_step_change(db):
    eng = AnomalyEngine(db)
    eng.set_rules({"thresholds": [], "rate": {}})  # isolate the statistical rule
    t = _warm(eng, "D3")
    assert db.alerts("D3", "open") == []
    fired, t = _feed(eng, "D3", t, 2.5, temp_c=75.0)
    assert "anomaly:temp_c" not in fired           # not yet: sustain_s is 3 s
    fired, t = _feed(eng, "D3", t, 1.0, temp_c=75.0)
    assert "anomaly:temp_c" in fired
    a = db.open_alert("D3", "anomaly:temp_c")
    assert a and a["metric"] == "temp_c"
    # other metrics stayed stable -> no anomaly alerts for them
    assert {x["rule"] for x in db.alerts("D3", "open")} == {"anomaly:temp_c"}


def test_ewma_ignores_a_bump_shorter_than_sustain_s(db):
    eng = AnomalyEngine(db)
    eng.set_rules({"thresholds": [], "rate": {}})
    t = _warm(eng, "D5")
    fired, t = _feed(eng, "D5", t, 1.5, vibration_g=1.8)   # a pothole: large, over in 1.5 s
    assert "anomaly:vibration_g" not in fired
    fired, t = _feed(eng, "D5", t, 5.0, vibration_g=0.5)
    assert db.alerts("D5", "all") == []
    fired, t = _feed(eng, "D5", t, 3.5, vibration_g=1.8)   # the same level held: a real change
    assert "anomaly:vibration_g" in fired


def test_ewma_decision_does_not_depend_on_telemetry_rate(db):
    # 2 s of deviation is 20 samples at 10 Hz and 10 at 5 Hz; neither is 3 s
    for dev, hz in (("D6", 10), ("D7", 5)):
        eng = AnomalyEngine(db)
        eng.set_rules({"thresholds": [], "rate": {}})
        t = _warm(eng, dev, hz=hz)
        fired, t = _feed(eng, dev, t, 2.0, hz=hz, temp_c=75.0)
        assert "anomaly:temp_c" not in fired, hz
        fired, t = _feed(eng, dev, t, 1.5, hz=hz, temp_c=75.0)
        assert "anomaly:temp_c" in fired, hz


def test_ewma_gate_separates_a_load_change_from_a_fault(db):
    eng = AnomalyEngine(db)
    eng.set_rules({"thresholds": [], "rate": {}})
    # the driver accelerates: rpm climbs and current follows it. Not an anomaly.
    t = _warm(eng, "D8")
    for i in range(60):
        fired = eng.on_telemetry("D8", dict(BASE, rpm=3000.0 + 40.0 * i, current_a=50.0 + 1.0 * i), t)
        assert "anomaly:current_a" not in fired
        t += 0.1
    assert db.alerts("D8", "all") == []
    # the same current at an unchanged rpm is one
    t = _warm(eng, "D9")
    fired, t = _feed(eng, "D9", t, 3.5, current_a=110.0)
    assert "anomaly:current_a" in fired
    # temperature is not gated: it is judged even while rpm moves
    t = _warm(eng, "D10")
    for i in range(40):
        fired = eng.on_telemetry("D10", dict(BASE, rpm=3000.0 + 40.0 * i, temp_c=75.0), t)
        t += 0.1
    assert "anomaly:temp_c" in fired


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


def test_threshold_overrides_per_device_and_hw_model(db):
    rules = {"thresholds": [{"name": "voltage_low", "metric": "voltage_v", "op": "<", "value": 80.0, "severity": "warn",
                             "overrides": [{"device_id": "J-BY-ID", "value": 4.5},
                                           {"hw_model": "ORIN-NANO", "value": 4.5, "severity": "critical"}]}]}
    eng = AnomalyEngine(db, rules=rules)
    db.upsert_device("J-BY-HW", hw_model="ORIN-NANO")
    t0 = 1000.0
    # A 5 V board is healthy under its override; the same reading on a 100 V device is an undervoltage.
    assert "voltage_low" not in eng.on_telemetry("J-BY-ID", dict(BASE, voltage_v=5.0), t0)
    assert "voltage_low" not in eng.on_telemetry("J-BY-HW", dict(BASE, voltage_v=5.0), t0)
    assert "voltage_low" in eng.on_telemetry("D1", dict(BASE, voltage_v=5.0), t0)
    # The override threshold still fires below it, with the override's severity.
    assert "voltage_low" in eng.on_telemetry("J-BY-HW", dict(BASE, voltage_v=4.2), t0 + 1)
    assert db.alerts("J-BY-HW", "open")[0]["severity"] == "critical"
    assert db.alerts("J-BY-ID", "open") == []
    # The default rule set carries the Jetson override.
    default_eng = AnomalyEngine(db)
    vl = next(r for r in default_eng.rules["thresholds"] if r["name"] == "voltage_low")
    assert vl["overrides"][0]["hw_model"] == "ORIN-NANO"
