# Component interfaces (contract between firmware / backend / sim)

## Firmware agent CLI  (`firmware/build/ecu_agent`)
```
ecu_agent --id ECU-0001 [--host 127.0.0.1] [--udp-port 8781] [--tcp-port 8782]
          [--hz 10] [--fw 1.0.0] [--hw SIM-MOTOR-A] [--profile city|highway|idle|stress]
          [--drop-rate 0.0] [--seed 42] [--duration 0] [--config path.cfg]
          [--fault FLAGNAME@START_S:DUR_S]...  [--log-level info] [--no-gps]
```
* `--drop-rate` : agent-side simulated UDP packet loss (0..1).
* `--duration`  : seconds to run then exit 0 (0 = forever).
* `--fault OVERTEMP@30:20` : inject flag at t=30s for 20s (repeatable). Fault names as in protocol.md.
* `--config` : key=value file overriding defaults (hz, rpm_limit, temp_limit_c, current_limit_a, log_level). CONFIG_SET from backend overrides at runtime and is persisted to `<config>.applied` next to it (if --config given).
* Exit codes: 0 ok, 2 bad args. Never exits on network failure — reconnects forever.
* Logs to stderr as `[ts] [level] msg`; also ships LOG frames over TCP when connected (level >= configured log_level).

## Backend HTTP API (FastAPI, :8780)  — UDP :8781, TCP :8782
```
GET  /health                                  {"status":"ok","devices_online":N,"uptime_s":..}
GET  /api/stats                               fleet counters: devices, frames, bad_crc, loss_pct, alerts_open
GET  /api/devices                             [{device_id, state, online, last_seen, fw_version, hw_model, config_version, loss_pct, latest:{rpm,...}, open_alerts}]
GET  /api/devices/{id}                        device detail incl. latest telemetry, stats, config, fw
GET  /api/devices/{id}/telemetry?since=ISO&until=ISO&limit=2000&step=1s|10s|1m   time-series rows (raw or rollup)
GET  /api/devices/{id}/logs?limit=200
GET  /api/devices/{id}/config                 current config (versioned)
PUT  /api/devices/{id}/config                 body {telemetry_hz, log_level, rpm_limit, temp_limit_c, current_limit_a} → pushes CONFIG_SET, bumps version
GET  /api/firmware                            registry [{version, notes, size_bytes, crc, created_at}]
POST /api/firmware                            {version, notes, size_bytes}
POST /api/devices/{id}/firmware               {version} → pushes FW_UPDATE; returns job id; GET /api/devices/{id}/firmware/status
POST /api/devices/{id}/fault                  {set:[..names], clear:[..], duration_ms} → FAULT_INJECT
POST /api/devices/{id}/cmd                    {cmd:"reboot"|"clear_faults"|"request_hello"}
GET  /api/alerts?device=&status=open|resolved|all&limit=
POST /api/alerts/{id}/ack
GET  /api/rules  /  PUT /api/rules            threshold + anomaly rules
POST /api/devices/{id}/diagnose               AI-assisted diagnosis {summary, probable_causes[], recommended_actions[], confidence, model}
GET  /api/events/stream                       SSE: telemetry | alert | device | log events (JSON per event, `event:` typed)
GET  /                                        dashboard UI
```
Storage: SQLite WAL at `backend/data/telemetry.db` (env `ECU_DB_PATH`). Env: `ECU_HTTP_PORT`, `ECU_UDP_PORT`, `ECU_TCP_PORT`, `ANTHROPIC_API_KEY` (optional — heuristic diagnosis fallback).

## SIL simulator (`sim/`)
* `sim/scenarios/*.yml` — fleet definitions: devices (id, profile, fw, drop_rate, seed) + timeline of fault injections / config pushes / OTA / expected alerts.
* `python sim/run_sil.py scenarios/thermal_runaway.yml [--backend http://localhost:8780] [--agent firmware/build/ecu_agent]` — spawns agents, drives the timeline via the backend API, asserts expectations, exits non-zero on failure. Prints a report.
* `python sim/lossy_proxy.py --listen 9781 --forward 127.0.0.1:8781 --drop 0.1 --delay-ms 50 --jitter-ms 20 --reorder 0.05` — UDP chaos proxy.
