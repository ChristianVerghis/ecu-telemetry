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

## Next
- [ ] Push to GitHub (private repo ChristianVerghis/ecu-telemetry) and watch CI go green
- [ ] Real hardware target: port agent to an ESP32/STM32 (lwIP sockets) reading a real motor
- [ ] MQTT transport option alongside raw UDP
- [ ] Downsampled long-range charts (rollup-backed 7d view)
- [ ] Alert notifications (email/Slack webhook)
