# ecu-telemetry build log

**Update 2026-09-01**: scaffolded the repo layout.

**Update 2026-09-01**: built the whole chain in one session.
- `docs/protocol.md` + `docs/interfaces.md` written first as the contract; firmware, backend and sim were then built in parallel against it.
- `firmware/`: dependency-free C++17 agent (CMake, 15 CTest tests). Deterministic plant model (motor/battery/thermal/vibration/GPS, 4 drive profiles, 8 fault flags), UDP telemetry with seq + CRC-32, select()-based TCP control client, backoff reconnect, simulated OTA with reboot, versioned config persisted to `<cfg>.applied`, LOG frame shipping.
- `backend/`: FastAPI + asyncio UDP/TCP servers, SQLite WAL with batched inserts + 1-min rollups + retention, threshold/EWMA/rate-of-change alert engine with dedupe + auto-resolve, Claude-backed diagnostics with heuristic fallback, SSE, vanilla-JS control-room dashboard. 24 pytest tests.
- `sim/`: SIL runner with 6 YAML scenarios + expectations, lossy UDP proxy with live control endpoint, fleet launcher. `tests/integration`: real agent ↔ real backend.
- `docker-compose.yml` (backend + 4 ECUs, `chaos` profile), GitHub Actions CI (firmware/backend/integration/SIL/docker) + tagged release workflow, Makefile.
- Integration fixes: aligned undervoltage rule to the firmware's 96 V pack (was 48 V), `pythonpath` in pytest.ini so tests run from repo root, thermal_runaway scenario timing relaxed (60 s thermal time constant means recovery takes ~40 s).
- Not verified locally: Docker builds (daemon not running). GitHub remote not yet created.
