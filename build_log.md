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

**Update 2026-11-05**: demo-ready pass started (plan in `GOALS.md` → "Demo-ready").
- D1 CI: `sim/run_sil.py` named per-process logs after the child name (`agent:ECU-0001`) and `actions/upload-artifact@v4` rejects colons, so both `sil` jobs failed at the upload step on every push to `main` since 09-15 while the scenarios themselves passed. Log filenames now use `-`. The fix sat on `nightly/2026-09-21` and was never merged.
- D1 CI, second cause: with the upload fixed, `sil (thermal_runaway)` still failed 6/7. The "healthy before the fault" check sat at `at_s: 15`, the same instant `OVERTEMP@15:25` begins, so it raced the fault and lost on the CI runner. Moved to `at_s: 10`; 7/7 locally.
- D1 CI green on `main` (run 42081749158), first time since 09-15. README brought up to date: status as of 11-05, test counts, a "Real hardware target" section with the Jetson thermal event and the two changes it forced (hysteresis, per-device rule overrides).
- D1 nightly branches: the 7-day chart range is in (`nightly/2026-09-25`, plus the rollup query test from `nightly/2026-09-09`); it reads the 1-minute rollups, which outlive the 24 h raw retention. MQTT stays on `nightly/2026-10-01`: it needs a broker and is not on the demo path. Deleted four local-only branches that were earlier attempts at the same two features (5e475fe, 6aab87e, 8e59ce4, c987616); c987616's `range=` API shorthand was not carried over.
- D2 `make demo`: `sim/scenarios/demo.yml` through the SIL runner with three new options. `--ports auto` uses 8780-8782 when free, else 8790+ (so it runs next to a dev backend); `--hold` keeps backend and fleet up after the timeline until Ctrl-C; timeline entries can carry `say:` narration. Scratch database under `sim/reports/`, wiped each start. Also fixed: `--start-backend` with a backend already on the port used to pass its health check against the old backend and run on its old database; it now refuses.
