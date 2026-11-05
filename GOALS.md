# ecu-telemetry — goals

A portfolio-grade miniature connected-vehicle platform that exercises the whole embedded→cloud chain.

## Core chain (v0.1)
- [x] Simulated ECU physics model (RPM, temperature, current, voltage, vibration, GPS/speed, fault states)
- [x] C++17 telemetry agent, dependency-free, CMake + CTest
- [x] Binary wire protocol (CRC32, seq numbers) over UDP telemetry + TCP control channel
- [x] FastAPI ingestion service with asyncio UDP/TCP servers
- [x] SQLite time-series storage with 1-minute rollups and retention
- [x] Live dashboard (fleet + device detail, SSE)
- [x] Alerts: threshold rules + EWMA z-score anomaly detection, dedupe/auto-resolve

## Interesting parts (v0.2)
- [x] Firmware versioning + simulated OTA (FW_UPDATE / FW_ACK / reboot / HELLO)
- [x] Versioned device configuration pushed over control channel
- [x] Remote logging (LOG frames → per-device log store)
- [x] Packet loss measurement + reconnection with backoff
- [x] Unit / API / integration tests
- [x] Containerized backend + fleet (docker compose)
- [x] CI/CD (GitHub Actions: firmware, backend, integration, SIL, docker, release)
- [x] SIL environment with scenario YAML + fault injection + lossy UDP proxy
- [x] AI-assisted diagnostics (Claude, heuristic fallback)

## Jetson week — real hardware target (2026-10-27 → 2026-11-02)
Board: Jetson Orin Nano Super (`ssh jetson`, Tailscale). Backend stays on the Mac (:8780, ingest UDP :8781 / TCP :8782 reachable over Tailscale at 100.95.205.56).

### Day 1 · Tue 10-27 · the Jetson is a device
- [ ] Build `ecu_agent` natively on the Jetson (arm64, gcc 13) and pass CTest there
- [ ] Run it as `ECU-JETSON-01` against the Mac backend over Tailscale; device shows live in the dashboard
- [ ] Keep it running: user systemd service on the Jetson (restart on failure, starts at boot)
- [ ] `make jetson-deploy` (rsync + build + restart) so a code change reaches the board in one command
- [ ] Note arm64 build + deploy in `docs/hardware.md`

### Day 2 · Wed 10-28 · real signals
- [x] `--source jetson`: temperature from `/sys/class/thermal`, VDD_IN voltage/current from the INA3221 power monitor, fan duty as rpm, so temp/voltage/current/rpm are measured, not simulated
- [x] Thermal-rule alert fires from a real event (6 cores + trtexec, 52 °C limit) and auto-resolves; per-device rule overrides in the backend so the 5 V rail is not an undervoltage
- [x] Unit test for the sysfs reader with fixture files (`sysfs_sensors`, `agent_runs_sysfs`, `cli_bad_source`)

### Day 3 · Thu 10-29 · CAN
- [ ] `vcan0` on the Jetson + `sim/can_motor.py` publishing motor frames (RPM, temp, current) on it
- [ ] `--source can`: SocketCAN reader with a small ID→signal map (`docs/can-map.md`); agent streams what it hears on the bus
- [ ] (owner) Order a 3.3 V CAN transceiver board (SN65HVD230) for the 40-pin header so `can0` goes physical next week

### Day 4 · Fri 10-30 · fleet, OTA and loss on a real network
- [ ] OTA push + reboot to the Jetson agent from the dashboard (service restart = reboot), HELLO comes back with the new version
- [ ] SIL scenario that includes the Jetson device alongside simulated ones; passes
- [ ] Packet-loss and reconnect numbers over Wi-Fi vs Tailscale relay; write them down

### Day 5 · Sat/Sun 10-31/11-01 · ship it
- [ ] Verify the release workflow's arm64 image runs on the Jetson via `docker --runtime nvidia` (and plain runc)
- [ ] README "Real hardware target" section with a dashboard screenshot of the live board; build_log; portfolio blurb
- [ ] Tag a release; CI green

## Demo-ready — something to show and explain in an interview (started 2026-11-05)
The demo must run on the Mac alone; the Jetson is a bonus when it is reachable. Work top to bottom, one item per turn, `make test` before each commit.

### D1 · the repo looks alive
- [x] CI green on `main`: bring over the SIL log-filename fix from `nightly/2026-09-21` (440e887); `main` has been red since 09-15 only because of the artifact upload step
- [x] README status is from 09-15 and still says "no real hardware target": update Status, add the Jetson section with the real thermal event from `docs/hardware.md`
- [x] Decide the nightly branches: 7-day chart merged (`nightly/2026-09-25` + the rollup test from `nightly/2026-09-09`); MQTT (`nightly/2026-10-01`) stays on its branch, it needs a broker and is not on the demo path; four local-only duplicates deleted

### D2 · one command, one story
- [x] `make demo`: fresh demo database (not `backend/data/telemetry.db`, which carries stale devices and 8 open `offline` alerts), backend + 6-device fleet, prints the dashboard URL
- [x] Scripted storyline, about 3 minutes, driven through the REST API like a SIL scenario: healthy fleet → thermal fault → alert opens → diagnostics → config push lowers the limit → OTA rollout → lossy link (loss % climbs) → recovery, alerts auto-resolve
- [x] Fix what the story would trip over: the EWMA anomaly rule was 22 of the 32 alerts in a demo run, all on healthy devices; it now holds for 3 s instead of 3 samples and is gated on the operating point, 0 noise alerts in the same run. Checked and not bugs: the device page charts fill in on first load; `offline` alerts are one per offline device and close on reconnect
- [x] `make demo` passes as a test (`make demo-check`, 21 expectations) and runs in CI as the third `sil` job

### D3 · things to point at
- [x] Dashboard screenshots + a short GIF of the storyline in the README (`tools/capture.mjs` drives headless Chrome over the DevTools protocol; the `--screenshot` flag fires before the charts have data)
- [x] `docs/demo.md`: the walkthrough, what to say at each step, and the design questions an interviewer will ask (why UDP + TCP, loss from sequence gaps, CRC, SQLite WAL + rollups, EWMA vs thresholds, the hysteresis story, what SIL with real binaries buys, what is simulated, security)
- [ ] Golden frames: one file of hex frames that both the C++ and the Python codec tests decode and re-encode. The README used to claim "shared byte vectors"; there were none (wording corrected 11-05)
- [ ] (owner) Diagnostics with a model: `backend/app/diagnostics.py` defaults to model id `claude-opus-5`; check it against the current model list before setting `ANTHROPIC_API_KEY` for a demo. Never run with a key so far
- [x] Numbers to quote (`make bench`, `docs/measurements.md`): 10,000 frames/s with no loss on one core; fault to alert 91 ms median; transit 0.7 ms; measured loss within about a point of injected from 5 to 50 %

### D4 · the real device (needs the Jetson reachable)
- [ ] (owner) Re-authenticate Tailscale SSH and run `deploy/jetson/install.sh` on the board once
- [ ] `ECU-JETSON-1` live in the demo fleet next to the simulated devices; storyline has an optional real-board step (load the cores, real OVERTEMP)
- [ ] OTA push to the real service from the dashboard, HELLO comes back with the new version (Jetson week Day 4)
- [ ] `vcan0` + `sim/can_motor.py` + `--source can` (Jetson week Day 3), if CAN is worth showing for the role

## Next
- [x] Real hardware target (in progress above, Jetson first; ESP32/STM32 port stays listed below)
- [ ] Publish to GitHub and watch CI go green
- [ ] Real hardware target: port agent to an ESP32/STM32 (lwIP sockets) reading a real motor
- [ ] MQTT transport option alongside raw UDP
- [x] Downsampled long-range charts (rollup-backed 7d view)
- [ ] Alert notifications (email/Slack webhook)
