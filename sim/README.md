# SIL — software-in-the-loop simulation

The `sim/` directory closes the loop around the real components: it launches the
**actual firmware binary** (`firmware/build/ecu_agent`) as many times as a scenario
has devices, points them at the **actual backend** (FastAPI, UDP :8781 / TCP :8782)
and then drives the system from the outside through the backend's HTTP API — fault
injection, config pushes, OTA, reboots, network chaos — while asserting what the
backend reports. Nothing is mocked; only the network can be made lossy.

```
                     ┌──────────────┐  UDP TELEMETRY   ┌───────────────┐
  run_sil.py ──spawn─►  ecu_agent   ├─────────────────►│               │
     │               │  (per device)│  TCP control     │   backend     │
     │               └──────────────┘◄────────────────►│ (uvicorn)     │
     │               ┌──────────────┐  UDP (impaired)  │               │
     ├──spawn────────►  ecu_agent   ├──► lossy_proxy ──►│               │
     │               └──────────────┘                  └───────┬───────┘
     │  timeline: POST /fault, PUT /config, POST /firmware ... │ HTTP API
     └──expect:  GET /api/devices, /api/alerts, /diagnose ◄────┘
```

Dependencies: Python 3.13 stdlib + `pyyaml` + `httpx` (both are in
`backend/requirements.txt`, so `backend/.venv/bin/python` works).

## Running

```sh
# start everything (backend from backend/, agents, proxy) and run a scenario
python sim/run_sil.py sim/scenarios/thermal_runaway.yml --start-backend

# against an already-running backend
python sim/run_sil.py sim/scenarios/baseline_fleet.yml --backend http://localhost:8780

# just check scenario files
python sim/run_sil.py --validate sim/scenarios/*.yml

# via make
make sil SCENARIO=sim/scenarios/cascade.yml
```

Flags: `--agent PATH` (default `firmware/build/ecu_agent`), `--via-proxy ECU-0002,ECU-0003`
(route extra devices through the chaos proxy), `--grace 5` (seconds an expectation is
polled before it is declared failed), `--fail-fast`, `--no-color`.

Output: a coloured PASS/FAIL table on stdout and a JSON report at
`sim/reports/<scenario>-<UTC timestamp>.json`. Child process logs land in
`sim/reports/logs/<scenario>-<child>.log`. Exit code `0` = every expectation passed,
`1` = at least one failed or a fatal error, `2` = bad arguments / invalid scenario.
All children (backend, proxy, agents) are killed on exit — including on Ctrl-C.

### How a run proceeds

1. Validate the scenario against the schema below.
2. `--start-backend`: run `python -m uvicorn app.main:app` from `backend/` (using
   `backend/.venv/bin/python` if it exists) with `ECU_HTTP_PORT/ECU_UDP_PORT/ECU_TCP_PORT`
   from the scenario and a scratch `ECU_DB_PATH` under `sim/reports/`, then wait for `/health`.
   Otherwise just check `/health` on the configured URL.
3. If any device has `via_proxy: true` (or `--via-proxy`), start `lossy_proxy.py` with the
   scenario's `proxy` block and wait for its control endpoint.
4. Spawn one `ecu_agent` per device with the CLI from `docs/interfaces.md`.
   `t = 0` is the moment the last agent has been spawned.
5. Merge `timeline` and `expect` into one time-ordered schedule and walk it in real time
   (agents are real-time, so there is no `--speed`). Actions call the HTTP API; expectations
   are evaluated at `at_s` and re-polled every 0.5 s for up to `--grace` seconds.
6. Print the table, write the report, kill all children.

## Scenario schema

```yaml
name: thermal_runaway                 # used for report file names
description: free text
duration_s: 100                       # agents get --duration duration_s+10
backend: {http: "http://127.0.0.1:8780", udp_port: 8781, tcp_port: 8782}
proxy:                                # optional — required if any device has via_proxy
  {listen: 9781, control_port: 9790, drop: 0.1, delay_ms: 40, jitter_ms: 20, reorder: 0.05, dup: 0}
devices:
  - id: ECU-0001                      # ASCII, <= 12 chars (protocol char[12])
    profile: city|highway|idle|stress
    fw: 1.0.0                         # MAJOR.MINOR.PATCH
    hw: SIM-MOTOR-A
    hz: 10                            # telemetry rate 1..100
    drop_rate: 0.0                    # agent-side simulated UDP loss 0..1
    seed: 42
    faults: ["OVERTEMP@20:40"]        # FLAG@START_S:DUR_S, passed as --fault (repeatable)
    via_proxy: false                  # send UDP through lossy_proxy instead of the backend
timeline:                             # things run_sil does TO the system
  - {at_s: 30, action: fault,             device: ECU-0001, params: {set: [ENCODER_FAULT], clear: [], duration_ms: 0}}
  - {at_s: 70, action: clear_fault,       device: ECU-0001, params: {clear: [ENCODER_FAULT]}}
  - {at_s: 15, action: config,            device: ECU-0001, params: {telemetry_hz, log_level, rpm_limit, temp_limit_c, current_limit_a}}
  - {at_s: 10, action: firmware_register,                   params: {version: "1.1.0", notes: "...", size_bytes: 1000000}}
  - {at_s: 15, action: firmware_deploy,   device: ECU-0001, params: {version: "1.1.0"}}
  - {at_s: 55, action: cmd,               device: ECU-0001, params: {cmd: reboot|clear_faults|request_hello}}
  - {at_s: 40, action: proxy_set,                           params: {drop: 0.5, delay_ms: 100}}   # POST /set on the proxy
expect:                               # things run_sil checks ABOUT the system
  - {at_s: 30, check: alert_open,             device: ECU-0001, params: {match: OVERTEMP}}      # match: substring or list, case-insensitive, over the whole alert JSON
  - {at_s: 85, check: alert_resolved,         device: ECU-0001, params: {match: [temp, OVERTEMP]}}
  - {at_s: 15, check: device_state,           device: ECU-0001, params: {online: true, has_telemetry: true, state: [DEGRADED, FAULT], fault_flags_any: [OVERTEMP], uptime_lt_s: 30}}
  - {at_s: 30, check: device_fw,              device: ECU-0001, params: {version: "1.1.0"}}
  - {at_s: 22, check: config_version,         device: ECU-0001, params: {min: 1, acked: true}}
  - {at_s: 25, check: loss_pct_gt,            device: ECU-0001, params: {value: 10}}            # invert: true => loss_pct <= value
  - {at_s: 45, check: diagnosis_nonempty,     device: ECU-0001, params: {}}                     # POST /diagnose -> probable_causes non-empty
  - {at_s: 40, check: no_alerts_of_severity,  params: {severity: critical}}                     # device optional; scopes to that device
```

Every `params` key shown for `device_state` is optional; only the ones given are asserted.
`fw_version` may come back as `"1.1.0"` or the packed `u32` — both are handled.
Alert matching is deliberately loose (substring over the serialised alert) so scenario
files don't have to know the backend's exact alert `type` strings — use the fault-flag
name (`OVERTEMP`, `ENCODER`), a metric (`temp`), or `["loss", "comm"]`.

Validation (`--validate`) rejects unknown actions/checks/fault names/profiles, device ids
longer than 12 chars, references to unknown devices, `proxy_set` without a `proxy` block,
etc. It warns (does not fail) about events scheduled after `duration_s`.

## Scenarios shipped

| file | what it proves |
|------|----------------|
| `baseline_fleet.yml` | 6 healthy devices, mixed profiles/rates/fw, all online with telemetry, zero critical alerts |
| `thermal_runaway.yml` | `OVERTEMP@20:40` on one device → flag alert + temp-threshold alert, DEGRADED/FAULT state, both auto-resolve |
| `lossy_link.yml` | 25 % agent-side loss + one device through `lossy_proxy` (loss raised to 50 % mid-run via `proxy_set`) → loss_pct tracks, comm/loss alerts open, clean device stays at 0 |
| `ota_rollout.yml` | register fw 1.1.0, staggered deploy to 3/4 devices → `fw_version` updates after simulated download + reboot + HELLO; 4th untouched |
| `config_push.yml` | PUT a low `temp_limit_c` → `config_version` bumps, ack observed, temp alert fires with no fault injected; restore → alert resolves |
| `cascade.yml` | UNDERVOLTAGE (agent schedule) + ENCODER_FAULT (API) + reboot cmd → DEGRADED/FAULT, both alerts, uptime reset, `/diagnose` returns probable causes |

## Adding a scenario

1. Copy the closest existing file in `sim/scenarios/` and rename `name:`.
2. Describe the fleet under `devices:`; give every device a distinct `seed` so runs are
   reproducible, and keep ids ≤ 12 ASCII chars.
3. Put the stimuli in `timeline:` and what must be observable in `expect:`. Leave slack
   between the two — the agent needs a few seconds to apply a fault and the backend needs a
   couple of telemetry frames to evaluate rules; `--grace` (5 s) covers jitter, not design.
4. `python sim/run_sil.py --validate sim/scenarios/your_scenario.yml`.
5. `python sim/run_sil.py sim/scenarios/your_scenario.yml --start-backend` and read the
   table; child logs are in `sim/reports/logs/` when something is off.
6. To run it in CI, add it to the `sil` job's matrix in `.github/workflows/ci.yml`.

## Other tools

* **`lossy_proxy.py`** — UDP chaos proxy. `--drop --delay-ms --jitter-ms --reorder --dup`,
  optional `--control-port` exposing `GET /health`, `GET /stats`, `POST /set {"drop":0.5,...}`.
  `--duration` and `--stats-every` help when using it standalone.
* **`fleet.py`** — `python sim/fleet.py --n 8 --profiles city,highway --drop 0.02` launches N
  agents against a running backend for dashboard demos; Ctrl-C stops them all.
