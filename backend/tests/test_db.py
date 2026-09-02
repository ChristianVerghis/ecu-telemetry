import time


def test_telemetry_buffer_flush_query_rollup(db):
    now = time.time()
    base = {"rpm": 1000.0, "temp_c": 50.0, "current_a": 10.0, "voltage_v": 96.0, "vibration_g": 0.1,
            "speed_kph": 20.0, "lat": 1.0, "lon": 2.0, "fault_flags": 0, "state": 1}
    t0 = (int(now) // 60) * 60 - 600  # 10 min ago, minute-aligned
    for i in range(100):
        row = dict(base, temp_c=50.0 + i)
        db.buffer_telemetry("ECU-0001", t0 + i * 0.6, i, row)
    assert db.flush() == 100
    assert db.flush() == 0
    raw = db.telemetry("ECU-0001", since=t0 - 1, until=t0 + 100, limit=5000)
    assert len(raw) == 100 and raw[0]["temp_c"] == 50.0 and raw[-1]["temp_c"] == 149.0
    n = db.rollup(upto=now)
    assert n >= 1
    rows = db.q("SELECT * FROM telemetry_rollup_1m WHERE device_id='ECU-0001' ORDER BY bucket")
    assert rows[0]["n"] == 100 and rows[0]["temp_c_min"] == 50.0 and rows[0]["temp_c_max"] == 149.0
    assert abs(rows[0]["temp_c_avg"] - 99.5) < 1e-6
    r1m = db.telemetry("ECU-0001", since=t0 - 1, until=t0 + 100, step="1m")
    assert r1m and r1m[0]["n"] == 100
    r10 = db.telemetry("ECU-0001", since=t0 - 1, until=t0 + 100, step="10s")
    assert len(r10) == 6
    st = db.metric_stats("ECU-0001", window_s=100000)
    assert st["n"] == 100 and st["temp_c"]["max"] == 149.0
    # retention prune removes old rows
    db.retention_h = 0.0001
    assert db.prune() == 100


def test_devices_config_alerts_firmware(db):
    db.upsert_device("ECU-0001", hw_model="SIM", fw_version="1.0.0")
    db.upsert_device("ECU-0001", state=2)
    d = db.get_device("ECU-0001")
    assert d["hw_model"] == "SIM" and d["state"] == 2
    c1 = db.new_config("ECU-0001", 10, 1, 6000, 90, 120)
    c2 = db.new_config("ECU-0001", 20, 0, 5000, 80, 100)
    assert (c1["version"], c2["version"]) == (1, 2)
    db.ack_config("ECU-0001", 2, "applied")
    assert db.current_config("ECU-0001")["ack_status"] == "applied"
    assert len(db.config_history("ECU-0001")) == 2

    a = db.create_alert("ECU-0001", "temp_high", "temp_c", 95.0, "warn", "hot")
    assert db.open_alert("ECU-0001", "temp_high")["id"] == a["id"]
    db.update_alert(a["id"], status="acked", acked_at=time.time())
    assert db.open_alert("ECU-0001", "temp_high") is not None  # acked still counts as open
    db.update_alert(a["id"], status="resolved", resolved_at=time.time())
    assert db.open_alert("ECU-0001", "temp_high") is None
    assert db.alerts(status="resolved")[0]["id"] == a["id"]
    assert db.open_alert_counts() == {}

    fw = db.firmware_add("1.1.0", "notes", 500000, 123)
    assert db.firmware_list()[0]["version"] == "1.1.0"
    job = db.fw_job_create("ECU-0001", "1.1.0", "1.0.0")
    db.fw_job_update(job["id"], "accepted")
    db.fw_job_update(job["id"], "applied")
    j = db.fw_job_latest("ECU-0001")
    assert j["status"] == "applied" and [h["status"] for h in j["history"]] == ["pending", "accepted", "applied"]

    log = db.add_log("ECU-0001", 2, "warn msg")
    assert db.logs("ECU-0001")[0]["id"] == log["id"]
    db.set_rules({"x": 1})
    assert db.get_rules() == {"x": 1}
    db.save_stats("ECU-0001", {"frames_rx": 5, "loss_pct": 1.5})
    assert db.load_stats()["ECU-0001"]["loss_pct"] == 1.5
    dg = db.save_diagnosis("ECU-0001", "heuristic", {"summary": "ok"}, {})
    assert db.diagnoses("ECU-0001")[0]["id"] == dg["id"]
