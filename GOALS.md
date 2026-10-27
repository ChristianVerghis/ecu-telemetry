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
- [ ] `--source jetson`: temperature from `/sys/class/thermal`, VDD_IN voltage/current from the INA3221 power monitor, CPU/GPU load as motor load, so temp/voltage/current/load fields are measured, not simulated
- [ ] Thermal-rule alert fires from a real event (run `jetson cuda` to heat the SoC) and auto-resolves
- [ ] Unit test for the sysfs reader with fixture files

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

## Next
- [x] Real hardware target (in progress above, Jetson first; ESP32/STM32 port stays listed below)
- [ ] Publish to GitHub and watch CI go green
- [ ] Real hardware target: port agent to an ESP32/STM32 (lwIP sockets) reading a real motor
- [ ] MQTT transport option alongside raw UDP
- [ ] Downsampled long-range charts (rollup-backed 7d view)
- [ ] Alert notifications (email/Slack webhook)
